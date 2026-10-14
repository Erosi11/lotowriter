import copy
import datetime as dt
import json
from pathlib import Path

import docx
import pytest
from docx.oxml.ns import qn
from PIL import Image

from lotowriter.energy import load_energy_types
from lotowriter.generate import (check, default_review_date, generate, load_procedure,
                                 procedure_number)
from lotowriter.photos import prepare_photo

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "sample_procedure.json"
TEMPLATE = ROOT / "templates" / "EHS-003a_v1.docx"


@pytest.fixture(scope="module")
def sample():
    if not (ROOT / "samples" / "photos" / "main_disconnect.jpg").exists():
        pytest.skip("run tools/make_sample_photos.py first")
    return load_procedure(SAMPLE)


def all_text(document):
    parts = [p.text for p in document.paragraphs]
    for t in document.tables:
        for row in t.rows:
            for c in row.cells:
                parts.append(c.text)
    return "\n".join(parts)


def iso_table(document):
    return next(t for t in document.tables if t.rows[0].cells[0].text.startswith("Picture of Energy"))


# ------------------------------------------------------------ numbering & dates

def test_procedure_number_format():
    assert procedure_number("02", "PROD", "10045", 0) == "02-PROD-10045-V00"
    assert procedure_number("02", "WH", "A-12", 3) == "02-WH-A_12-V03"
    assert procedure_number("02", "FAC", " 77 ", 12) == "02-FAC-77-V12"


def test_review_date_defaults_to_one_year():
    assert default_review_date(dt.date(2026, 11, 1)) == dt.date(2027, 11, 1)
    assert default_review_date(dt.date(2028, 2, 29)) == dt.date(2029, 2, 28)


# ------------------------------------------------------------ completeness check

def test_check_flags_missing_photo_without_reason(sample):
    data = copy.deepcopy(sample)
    data["points"][0]["photos"] = []
    warnings = check(data, load_energy_types())
    assert any("E-1: no photo" in w for w in warnings)


def test_check_flags_review_date_beyond_one_year(sample):
    data = copy.deepcopy(sample)
    data["review_date"] = "2028-01-01"
    assert any("more than 1 year" in w for w in check(data, load_energy_types()))


def test_check_flags_unchecked_copied_point(sample):
    data = copy.deepcopy(sample)
    data["points"][1]["copy_status"] = "needs_check"
    assert any("E-2: copied" in w for w in check(data, load_energy_types()))


def test_sample_has_no_warnings(sample):
    assert check(sample, load_energy_types()) == []


# ------------------------------------------------------------ template

def test_template_has_no_instruction_text():
    text = all_text(docx.Document(TEMPLATE))
    for leftover in ("Insert picture", "List the positions", "Example:", "Pneumatic – 95 PSI",
                     "Enter type and magnitude"):
        assert leftover not in text


# ------------------------------------------------------------ generated document

@pytest.fixture(scope="module")
def generated(sample, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    result = generate(sample, out, pdf=False)
    return result, docx.Document(result.docx_path)


def test_output_named_by_procedure_number(generated):
    result, _ = generated
    assert result.docx_path.name == "02-PROD-10045-V00.docx"


def test_description_fields(generated, sample):
    _, d = generated
    top = d.tables[0]
    text = all_text(d)
    assert "Villa Rica" in text and "Production" in text
    assert "10045 – Die Cutter" in text
    assert sample["asset"]["oem"] not in text and sample["asset"]["model"] not in text
    # 9 points: 8 x 1 lock + M-1 with 2 locks + SS-1 with 0 locks
    assert any(c.text.strip() == "9" for row in top.rows for c in row.cells)


def test_one_row_per_point_in_order(generated, sample):
    _, d = generated
    rows = iso_table(d).rows[1:]
    assert len(rows) == len(sample["points"])
    for row, p in zip(rows, sample["points"]):
        energy_cell = row._tr.tc_lst[1]
        assert p["label"] in "".join(t.text or "" for t in energy_cell.iter(qn("w:t")))


def test_energy_cells_match_legend(generated, sample):
    _, d = generated
    types = load_energy_types()
    for row, p in zip(iso_table(d).rows[1:], sample["points"]):
        shd = row._tr.tc_lst[1].find(qn("w:tcPr") + "/" + qn("w:shd"))
        et = types[p["energy_type"]]
        assert (shd.get(qn("w:val")), shd.get(qn("w:color")), shd.get(qn("w:fill"))) == \
               (et.pattern, et.pattern_color, et.fill), p["label"]
        # Photo cell must not be shaded.
        assert row._tr.tc_lst[0].find(qn("w:tcPr") + "/" + qn("w:shd")) is None


def test_header_row_repeats_and_rows_do_not_split(generated):
    _, d = generated
    rows = iso_table(d).rows
    assert rows[0]._tr.find(qn("w:trPr") + "/" + qn("w:tblHeader")) is not None
    for row in rows[1:]:
        assert row._tr.find(qn("w:trPr") + "/" + qn("w:cantSplit")) is not None


def test_footer_values(generated):
    _, d = generated
    footer = "".join(t.text or "" for t in d.sections[0].footer._element.iter(qn("w:t")))
    for expected in ("02-PROD-10045-V00", "DRAFT", "November 1, 2026", "November 1, 2027", "Sample Owner"):
        assert expected in footer


def test_draft_has_watermark_active_does_not(sample, tmp_path):
    header = lambda path: docx.Document(path).sections[0].header._element.xml
    draft = generate(sample, tmp_path / "draft", pdf=False)
    assert "DRAFT - NOT FOR USE" in header(draft.docx_path)
    active = copy.deepcopy(sample)
    active["status"] = "active"
    result = generate(active, tmp_path / "active", pdf=False)
    assert "LotoWriterWatermark" not in header(result.docx_path)


def test_missing_photo_note_printed(generated):
    _, d = generated
    assert "PHOTO TO BE ADDED" in all_text(d)


# ------------------------------------------------------------ photos

def test_photo_rotation_and_metadata_stripped(tmp_path):
    src = tmp_path / "phone.jpg"
    im = Image.new("RGB", (400, 300), "red")  # stored landscape...
    exif = Image.Exif()
    exif[0x0112] = 6  # ...but camera says rotate 90 deg -> portrait
    exif[0x010F] = "PhoneMaker"  # camera make: any metadata must be dropped
    im.save(src, exif=exif)
    out, w_in, h_in = prepare_photo(src, tmp_path, "x", 2.2, 2.4)
    with Image.open(out) as res:
        assert res.size[0] < res.size[1]  # now portrait
        assert not res.getexif()
    assert w_in <= 2.2 + 1e-9 and h_in <= 2.4 + 1e-9
