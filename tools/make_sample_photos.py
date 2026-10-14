"""Create sample photos for samples/sample_procedure.json.

Uses the two real example photos embedded in the EHS-003a form for the
pneumatic point and draws labeled placeholders for the rest. One placeholder
is saved sideways with an EXIF orientation tag to exercise rotation handling.
"""
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "photos"


def font(size):
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def placeholder(name, text, size, color):
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline="white", width=8)
    d.multiline_text((size[0] / 2, size[1] / 2), "SAMPLE PHOTO\n" + text, fill="white",
                     font=font(size[0] // 14), anchor="mm", align="center", spacing=12)
    return im


def main(form_docx):
    OUT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(form_docx) as z:
        for member, name in (("word/media/image3.jpeg", "pneumatic_valve_1.jpg"),
                             ("word/media/image4.jpeg", "pneumatic_valve_2.jpg")):
            (OUT / name).write_bytes(z.read(member))

    specs = [
        ("main_disconnect.jpg", "E-1 Main disconnect\nMCC-2 Bucket 4", (1200, 1600), (120, 40, 40)),
        ("control_panel.jpg", "E-2 Control panel\nUPS breaker", (1600, 1200), (120, 40, 40)),
        ("hydraulic_valve.jpg", "H-1 Hydraulic\nisolation valve", (1200, 1600), (30, 40, 120)),
        ("steam_valve.jpg", "S-1 Steam supply\nball valve", (1200, 1600), (60, 40, 110)),
        ("platen_blocks.jpg", "M-1 Platen\nsafety blocks", (1600, 1200), (90, 40, 70)),
        ("heater_breaker.jpg", "T-1 Die heater\nbreaker", (1200, 1600), (150, 70, 20)),
        ("chem_valve.jpg", "C-1 Adhesive\nsupply valve", (1200, 1600), (120, 120, 20)),
        ("start_button.jpg", "SS-1 Operator\nstart station", (1600, 1200), (90, 90, 90)),
    ]
    for name, text, size, color in specs:
        im = placeholder(name, text, size, color)
        if name == "control_panel.jpg":
            # Store sideways + EXIF Orientation=6 (rotate 90 CW to view), like a phone photo.
            im = im.transpose(Image.Transpose.ROTATE_90)
            exif = Image.Exif()
            exif[0x0112] = 6
            im.save(OUT / name, "JPEG", quality=85, exif=exif)
        else:
            im.save(OUT / name, "JPEG", quality=85)
    print(f"Sample photos written to {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ROOT / "EHS-003a LOTO Machine-Specific Energy.docx")
