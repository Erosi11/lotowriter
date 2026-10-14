"""Fill the tagged EHS-003a template from procedure data and write DOCX (+ PDF).

Procedure data is a dict (see samples/sample_procedure.json for the format).
"""
import copy
import datetime as dt
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import docx
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Inches
from docxtpl import DocxTemplate, InlineImage, RichText

from .energy import load_energy_types
from .photos import prepare_photo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = ROOT / "templates" / "EHS-003a_v1.docx"

STATUS_LABELS = {
    "draft": "DRAFT",
    "ready_for_review": "READY FOR REVIEW",
    "printed_for_review": "PRINTED FOR REVIEW",
    "returned": "RETURNED WITH MARKUPS",
    "approved": "APPROVED - NOT YET ACTIVE",
    "active": "ACTIVE",
    "superseded": "SUPERSEDED",
}
WATERMARK_TEXT = {"superseded": "SUPERSEDED"}  # everything else not active -> DRAFT text
DRAFT_WATERMARK = "DRAFT - NOT FOR USE"

# Print sizes (inches). The photo column is ~2.4" wide including cell margins.
PHOTO_MAX_W = 2.2
PHOTO_MAX_H_SINGLE = 2.4
PHOTO_MAX_H_DOUBLE = 1.6
MAX_PHOTOS_PER_POINT = 2

CONTROL_FIELDS = [  # (data key, printed label)
    ("device", "Isolation device"),
    ("deenergize", "De-energize"),
    ("lock", "Lock"),
    ("stored_energy", "Stored energy"),
    ("verify", "Verify zero energy"),
    ("notes", "Note"),
]
CONTROL_SIZE = 22  # half-points (11 pt)
BODY_FONT = "Calibri"
ENERGY_LABEL_SIZE = 44  # half-points (22 pt), same as the form's example


@dataclass
class Result:
    docx_path: Path
    pdf_path: Path | None
    procedure_number: str
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def procedure_number(site_code, dept_code, asset_number, revision):
    """SITE-DEPT-ASSET-REV, e.g. 02-PROD-10045-V00. Dashes in the asset # become underscores."""
    asset = str(asset_number).strip().replace("-", "_")
    return f"{site_code}-{dept_code}-{asset}-V{int(revision):02d}"


def default_review_date(effective):
    """Effective date + 1 year (Feb 29 -> Feb 28)."""
    try:
        return effective.replace(year=effective.year + 1)
    except ValueError:
        return effective.replace(year=effective.year + 1, day=28)


def format_date(d):
    return f"{d.strftime('%B')} {d.day}, {d.year}" if d else ""


def parse_date(value):
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(value)


def rich(text, **fmt):
    """RichText with newlines turned into line breaks."""
    rt = RichText()
    if text:
        rt.add(str(text), **fmt)
    return rt


def lines(items, bullet="• ", numbered=False):
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if numbered:
        return "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1))
    return "\n".join(f"{bullet}{i}" for i in items)


# ---------------------------------------------------------------- validation

def assess(data, energy_types):
    """Completeness check -> (blocking, warnings).

    Blocking items stop a draft from being marked Ready for Review; warnings
    are shown but don't block. This is a drafting aid, not a safety check.
    """
    block, warn = [], []
    points = data.get("points") or []
    if not points:
        block.append("No isolation points entered.")
    for p in points:
        tag = p.get("label") or "(unlabeled point)"
        if p.get("energy_type") not in energy_types:
            block.append(f"{tag}: unknown energy type {p.get('energy_type')!r}.")
        if not str(p.get("magnitude", "") or "").strip():
            block.append(f"{tag}: magnitude missing (enter value or 'Unknown - verify').")
        for key, name in (("device", "isolation device / location"), ("verify", "verify zero energy")):
            if not str(p.get(key, "") or "").strip():
                block.append(f"{tag}: '{name}' missing.")
        if not p.get("photos") and not p.get("photo_missing_reason"):
            block.append(f"{tag}: no photo and no reason recorded.")
        if len(p.get("photos") or []) > MAX_PHOTOS_PER_POINT:
            warn.append(f"{tag}: only the first {MAX_PHOTOS_PER_POINT} photos are printed.")
        if p.get("copy_status") == "needs_check":
            block.append(f"{tag}: copied from another machine and not yet checked.")
        if p.get("energy_type") in ("hydraulic", "pneumatic") and not str(p.get("stored_energy", "") or "").strip()                 and "0 psi" not in str(p.get("verify", "")).lower():
            warn.append(f"{tag}: no stored-energy step on a {p['energy_type']} point.")
        if "unknown" in str(p.get("magnitude", "")).lower():
            warn.append(f"{tag}: magnitude marked unknown - reviewers must confirm.")
    if points and not any(p.get("energy_type") in ("electrical", "startstop") for p in points):
        warn.append("No Electrical or Start/Stop Switch point - confirm none applies.")
    if data.get("locks_required_override") is not None and not data.get("locks_override_reason"):
        block.append("# of locks overridden without a reason.")
    for key, name in (("effective_date", "Document Effective Date"), ("document_owner", "Document Owner")):
        if not data.get(key):
            block.append(f"Footer '{name}' is blank.")
    eff, rev = parse_date(data.get("effective_date")), parse_date(data.get("review_date"))
    if eff and rev:
        if rev < eff:
            block.append("Review date is earlier than the effective date.")
        elif rev > default_review_date(eff):
            warn.append("Review date is more than 1 year after the effective date (annual inspection).")
    return block, warn


def check(data, energy_types):
    """All completeness messages (blocking first), for the command line."""
    block, warn = assess(data, energy_types)
    return block + warn


# ---------------------------------------------------------------- context

def build_context(data, tpl, energy_types, work_dir):
    site, dept, asset = data["site"], data["department"], data["asset"]
    status = data.get("status", "draft")
    eff = parse_date(data.get("effective_date"))
    review = parse_date(data.get("review_date")) or (default_review_date(eff) if eff else None)
    points = data.get("points") or []

    # Energy summary grouped by type in isolation order.
    summary = {}
    for p in points:
        et = energy_types.get(p.get("energy_type"))
        if et:
            summary.setdefault(et.name, [])
            mag = str(p.get("magnitude", "")).strip()
            if mag and mag not in summary[et.name]:
                summary[et.name].append(mag)
    energy_summary = "\n".join(f"{name}: {', '.join(m)}" if m else name for name, m in summary.items())

    locks = data.get("locks_required_override")
    if locks is None:
        locks = sum(int(p.get("locks", 1)) for p in points)

    ctx_points = []
    for i, p in enumerate(points, 1):
        et = energy_types.get(p.get("energy_type"))
        label = rich(p.get("label", f"#{i}"), size=20, bold=True, color=et.text_color if et else None)
        label.add("\n")
        label.add(f"{et.name if et else p.get('energy_type')} – {p.get('magnitude', '')}".strip(" –"),
                  size=ENERGY_LABEL_SIZE, bold=True, color=et.text_color if et else None)

        photos = (p.get("photos") or [])[:MAX_PHOTOS_PER_POINT]
        max_h = PHOTO_MAX_H_SINGLE if len(photos) == 1 else PHOTO_MAX_H_DOUBLE
        images = []
        for n, ph in enumerate(photos, 1):
            path = Path(ph["file"] if isinstance(ph, dict) else ph)
            if not path.is_absolute():
                path = Path(data.get("_base_dir", ".")) / path
            marks = ph.get("annotations") if isinstance(ph, dict) else None
            out, w_in, h_in = prepare_photo(path, work_dir, f"p{i:02d}_{n}", PHOTO_MAX_W, max_h, annotations=marks)
            images.append({"image": InlineImage(tpl, str(out), width=Inches(w_in), height=Inches(h_in))})
        note = ""
        if not photos:
            note = "PHOTO TO BE ADDED"
            if p.get("photo_missing_reason"):
                note += f"\n({p['photo_missing_reason']})"

        control = []
        for key, caption in CONTROL_FIELDS:
            value = str(p.get(key, "") or "").strip()
            if value:
                rt = RichText()
                rt.add(f"{caption}: ", bold=True, size=CONTROL_SIZE, font=BODY_FONT)
                rt.add(value, size=CONTROL_SIZE, font=BODY_FONT)
                control.append(rt)
        if p.get("locks") and int(p["locks"]) > 1:
            control.append(rich(f"Locks at this point: {p['locks']}", size=CONTROL_SIZE, bold=True, font=BODY_FONT))

        ctx_points.append({
            "photos": images,
            "photo_note": rich(note, bold=True, size=22),
            "energy_label": label,
            "control_lines": control,
        })

    devices = lines(data.get("lockout_devices"))
    if data.get("group_lockout"):
        devices += ("\n" if devices else "") + f"Group lockout: {data['group_lockout']}"

    n = len(points)
    return {
        "site_name": site["name"],
        "department_name": dept["name"],
        "machine_equipment": f"{asset['number']} – {asset['machine_type']}",
        "tasks_covered": rich(lines(data.get("tasks_covered"))),
        "energy_summary": rich(energy_summary),
        "locks_required": locks,
        "affected_employees": rich(lines(data.get("affected_employees"))),
        "shutdown_procedure": rich(data.get("shutdown_procedure", "")),
        "isolate_text": rich(data.get("isolate_text") or
                             f"Isolate the {n} energy isolation point{'s' if n != 1 else ''} in the order listed in the table below."),
        "lockout_devices": rich(devices),
        "stored_energy": rich(data.get("stored_energy") or
                              "See “Stored energy” for each isolation point in the table below."),
        "verify_isolation": rich(data.get("verify_isolation") or
                                 "See “Verify zero energy” for each isolation point in the table below."),
        "servicing_notes": rich(data.get("servicing_notes", "")),
        "restart_steps": rich(lines(data.get("restart_steps"), numbered=True)),
        "points": ctx_points,
        "procedure_number": (f"{data['procedure_number_base']}-V{int(data.get('revision', 0)):02d}"
                             if data.get("procedure_number_base")
                             else procedure_number(site["code"], dept["code"], asset["number"], data.get("revision", 0))),
        "status_label": STATUS_LABELS.get(status, status.upper()),
        "effective_date": format_date(eff),
        "review_date": format_date(review),
        "document_owner": data.get("document_owner", ""),
    }


# ---------------------------------------------------------------- post-processing

def apply_energy_shading(document, points, energy_types):
    """Color each energy cell exactly like the legend (fill + pattern)."""
    table = next(t for t in document.tables if t.rows[0].cells[0].text.startswith("Picture of Energy"))
    point_rows = table.rows[1:]
    assert len(point_rows) == len(points), "isolation table row count mismatch"
    for row, p in zip(point_rows, points):
        et = energy_types.get(p.get("energy_type"))
        if not et:
            continue
        tc = row._tr.tc_lst[1]  # 2nd visible cell (row.cells counts grid columns)
        tcpr = tc.get_or_add_tcPr()
        for old in tcpr.findall(qn("w:shd")):
            tcpr.remove(old)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), et.pattern)
        shd.set(qn("w:color"), et.pattern_color)
        shd.set(qn("w:fill"), et.fill)
        # Schema order: shd comes before noWrap/tcMar/textDirection/tcFitText/vAlign/hideMark.
        anchor = None
        for tag in ("w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark"):
            anchor = tcpr.find(qn(tag))
            if anchor is not None:
                break
        if anchor is not None:
            anchor.addprevious(shd)
        else:
            tcpr.append(shd)


_SHAPETYPE = (
    '<v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" '
    'path="m@7,l@8,m@5,21600l@6,21600e"><v:formulas>'
    '<v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/><v:f eqn="sum 21600 0 @1"/>'
    '<v:f eqn="sum 0 0 @2"/><v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>'
    '<v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/><v:f eqn="if @0 @4 21600"/>'
    '<v:f eqn="mid @5 @6"/><v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>'
    '<v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/></v:formulas>'
    '<v:path textpathok="t" o:connecttype="custom" o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" '
    'o:connectangles="270,180,90,0"/><v:textpath on="t" fitshape="t"/>'
    '<v:handles><v:h position="#0,bottomRight" xrange="6629,14971"/></v:handles>'
    '<o:lock v:ext="edit" text="t" shapetype="t"/></v:shapetype>'
)


def add_watermark(document, text):
    """Diagonal semi-transparent text watermark on every page (Word-style)."""
    xml = (
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:w10="urn:schemas-microsoft-com:office:word"><w:pict>' + _SHAPETYPE +
        '<v:shape id="LotoWriterWatermark" o:spid="_x0000_s9001" type="#_x0000_t136" '
        'style="position:absolute;margin-left:0;margin-top:0;width:500pt;height:100pt;rotation:315;'
        'z-index:-251657216;mso-position-horizontal:center;mso-position-horizontal-relative:margin;'
        'mso-position-vertical:center;mso-position-vertical-relative:margin" '
        'o:allowincell="f" fillcolor="#c00000" stroked="f">'
        '<v:fill opacity=".25"/>'
        f'<v:textpath style="font-family:&quot;Calibri&quot;;font-size:1pt" string="{text}"/>'
        '<w10:wrap anchorx="margin" anchory="margin"/></v:shape></w:pict></w:r>'
    )
    for section in document.sections:
        header = section.header._element
        paragraphs = header.findall(qn("w:p"))
        paragraphs[-1].append(parse_xml(xml))


# ---------------------------------------------------------------- main entry

def generate(data, out_dir, template=DEFAULT_TEMPLATE, energy_config=None, pdf=True):
    energy_types = load_energy_types(energy_config) if energy_config else load_energy_types()
    warnings = check(data, energy_types)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tpl = DocxTemplate(str(template))
    with tempfile.TemporaryDirectory() as work:
        ctx = build_context(data, tpl, energy_types, work)
        tpl.render(ctx)
        rendered = Path(work) / "rendered.docx"
        tpl.save(rendered)

        document = docx.Document(rendered)
        apply_energy_shading(document, data.get("points") or [], energy_types)
        status = data.get("status", "draft")
        if status != "active":
            add_watermark(document, WATERMARK_TEXT.get(status, DRAFT_WATERMARK))
        docx_path = out_dir / f"{ctx['procedure_number']}.docx"
        document.save(docx_path)

    pdf_path = None
    if pdf:
        from .pdf import to_pdf
        try:
            pdf_path = to_pdf(docx_path)
        except RuntimeError as e:
            warnings.append(f"PDF not created: {e}")
    return Result(docx_path, pdf_path, ctx["procedure_number"], warnings)


def load_procedure(path):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_base_dir"] = str(path.parent)
    return data
