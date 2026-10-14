"""DOCX -> PDF conversion.

Production (Linux PC): LibreOffice headless (`soffice`).
Development on Windows: falls back to Microsoft Word via tools/word2pdf.ps1.
"""
import os
import shutil
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# LibreOffice can't run two conversions at once with the same user profile, so
# conversions take turns and use a profile folder owned by the app.
_lock = threading.Lock()


def to_pdf(docx_path, timeout=180):
    docx_path = Path(docx_path).resolve()
    pdf_path = docx_path.with_suffix(".pdf")
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    with _lock:
        if soffice:
            from .db import data_dir
            profile = (data_dir() / "lo-profile").resolve()
            subprocess.run(
                [soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                 "--convert-to", "pdf", "--outdir", str(docx_path.parent), str(docx_path)],
                check=True, timeout=timeout, capture_output=True,
            )
        elif os.name == "nt":
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "tools" / "word2pdf.ps1"), "-In", str(docx_path), "-Out", str(pdf_path)],
                check=True, timeout=timeout, capture_output=True,
            )
        else:
            raise RuntimeError("LibreOffice (soffice) not found; install it to produce PDFs.")
    if not pdf_path.exists():
        raise RuntimeError(f"converter ran but {pdf_path.name} was not produced")
    return pdf_path
