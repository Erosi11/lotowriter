import datetime as dt
import io

import docx
import pytest

from test_web import H, client, fill_ready, jpeg, new_procedure, prod_dept, sign_in  # noqa: F401  (fixtures)


@pytest.fixture()
def admin(client):
    sign_in(client)
    client.post("/api/me/password", json={"current": "Password1", "new": "Villa-Rica-2026"}, headers=H)
    return client


def act(c, rid, action, **body):
    return c.post(f"/api/revisions/{rid}/workflow", json={"action": action, **body}, headers=H)


def status(c, rid):
    return c.get(f"/api/revisions/{rid}").json()["status"]


def to_printed(c, rid):
    fill_ready(c, rid)
    assert c.post(f"/api/revisions/{rid}/status", json={"status": "ready_for_review"}, headers=H).status_code == 200
    r = act(c, rid, "print_for_review")
    assert r.status_code == 200, r.text
    return r


def to_active(c, rid, effective="2026-11-01"):
    to_printed(c, rid)
    assert act(c, rid, "approve", approved_by="J. Reviewer, EHS", approved_date=dt.date.today().isoformat()).status_code == 200
    r = act(c, rid, "activate", effective_date=effective)
    assert r.status_code == 200, r.text
    return r


def header_xml(c, doc_id):
    raw = c.get(f"/api/documents/{doc_id}/docx").content
    return docx.Document(io.BytesIO(raw)).sections[0].header._element.xml


# ------------------------------------------------------------------ the main path

def test_full_lifecycle(admin):
    rid = new_procedure(admin)
    printed = to_printed(admin, rid)
    rev = printed.json()["revision"]
    assert rev["status"] == "printed_for_review" and rev["printed_for_review_at"]
    assert rev["documents"][0]["status_at_print"] == "printed_for_review"
    assert "DRAFT - NOT FOR USE" in header_xml(admin, rev["documents"][0]["id"])

    # Frozen while reviewers have the paper copy.
    pid = rev["points"][0]["id"]
    assert admin.put(f"/api/points/{pid}", json={"magnitude": "x"}, headers=H).status_code == 409

    # Approval needs names and a sensible date.
    assert act(admin, rid, "approve", approved_by="", approved_date="2026-10-01").status_code == 400
    future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    assert act(admin, rid, "approve", approved_by="A", approved_date=future).status_code == 400
    ok = act(admin, rid, "approve", approved_by="J. Reviewer", approved_date=dt.date.today().isoformat())
    assert ok.json()["revision"]["status"] == "approved"
    assert ok.json()["revision"]["approval_recorded_by"] == "Administrator"

    active = act(admin, rid, "activate", effective_date="2026-11-01").json()["revision"]
    assert active["status"] == "active" and active["effective_date"] == "2026-11-01"
    assert active["review_date"] == "2027-11-01"
    final = active["documents"][0]
    assert final["status_at_print"] == "active"
    assert "LotoWriterWatermark" not in header_xml(admin, final["id"])  # no DRAFT on the final copy


def test_returned_with_markups_goes_back_to_editing(admin):
    rid = new_procedure(admin)
    to_printed(admin, rid)
    assert act(admin, rid, "return", note="").status_code == 400
    rev = act(admin, rid, "return", note="Reviewer: add breaker number to E-1").json()["revision"]
    assert rev["status"] == "returned" and rev["editable"]
    pid = rev["points"][0]["id"]
    assert admin.put(f"/api/points/{pid}", json={"device": "Breaker CB-7"}, headers=H).status_code == 200
    assert status(admin, rid) == "draft"


def test_withdraw_approval(admin):
    rid = new_procedure(admin)
    to_printed(admin, rid)
    act(admin, rid, "approve", approved_by="X", approved_date=dt.date.today().isoformat())
    rev = act(admin, rid, "withdraw_approval").json()["revision"]
    assert rev["status"] == "printed_for_review" and rev["approved_by"] == ""


def test_wrong_order_is_refused(admin):
    rid = new_procedure(admin)
    fill_ready(admin, rid)
    assert act(admin, rid, "print_for_review").status_code == 409   # still draft
    assert act(admin, rid, "approve", approved_by="X", approved_date="2026-10-01").status_code == 409
    assert act(admin, rid, "activate").status_code == 409
    assert act(admin, rid, "new_revision", reason="x").status_code == 409


# ------------------------------------------------------------------ revisions & supersede

def test_new_revision_and_supersede(admin):
    rid = new_procedure(admin)
    to_active(admin, rid)
    assert act(admin, rid, "new_revision", reason="").status_code == 400
    r1 = act(admin, rid, "new_revision", reason="Guard replaced; disconnect moved")
    assert r1.status_code == 200, r1.text
    v1 = r1.json()["revision"]
    assert v1["procedure_number"] == "02-PROD-10045-V01" and v1["status"] == "draft"
    assert v1["change_reason"] == "Guard replaced; disconnect moved"
    assert v1["effective_date"] is None
    assert len(v1["points"]) == 1 and len(v1["points"][0]["photos"]) == 1
    assert v1["points"][0]["copy_status"] == "n/a"
    # Only one revision in progress at a time.
    assert act(admin, rid, "new_revision", reason="again").status_code == 409
    # V00 remains Active until V01 is activated.
    assert status(admin, rid) == "active"
    v1_id = v1["id"]
    admin.put(f"/api/revisions/{v1_id}", json={"version": v1["version"], "effective_date": "2027-01-15"}, headers=H)
    admin.post(f"/api/revisions/{v1_id}/status", json={"status": "ready_for_review"}, headers=H)
    act(admin, v1_id, "print_for_review")
    act(admin, v1_id, "approve", approved_by="Y", approved_date=dt.date.today().isoformat())
    act(admin, v1_id, "activate")
    assert status(admin, rid) == "superseded"
    v1 = admin.get(f"/api/revisions/{v1_id}").json()
    assert v1["status"] == "active" and v1["effective_date"] == "2027-01-15"
    assert [h["status"] for h in v1["history"]] == ["active", "superseded"]
    # Superseded can't be edited.
    old = admin.get(f"/api/revisions/{rid}").json()
    assert not old["editable"]


def test_read_only_sees_active_while_new_draft_exists(admin):
    rid = new_procedure(admin)
    to_active(admin, rid)
    act(admin, rid, "new_revision", reason="update")
    admin.post("/api/admin/users", json={"username": "viewer", "role": "read_only", "temp_password": "Temp-pass-1"},
               headers=H)
    draft_photo = admin.get(f"/api/revisions/{rid}").json()["points"][0]["photos"][0]["file_id"]
    # A photo that exists only on a draft:
    v1 = admin.get("/api/procedures").json()[0]
    pid = admin.get(f"/api/revisions/{v1['revision_id']}").json()["points"][0]["id"]
    new_ph = admin.post(f"/api/points/{pid}/photos", files={"file": ("n.jpg", jpeg(color="blue"), "image/jpeg")},
                        headers=H).json()["id"]
    draft_only = next(ph["file_id"] for ph in admin.get(f"/api/revisions/{v1['revision_id']}").json()["points"][0]["photos"]
                      if ph["id"] == new_ph)
    admin.post("/api/logout", headers=H)
    sign_in(admin, "viewer", "Temp-pass-1")
    admin.post("/api/me/password", json={"current": "Temp-pass-1", "new": "Viewer-pass-2"}, headers=H)
    rows = admin.get("/api/procedures").json()
    assert [r["procedure_number"] for r in rows] == ["02-PROD-10045-V00"]
    rev = admin.get(f"/api/revisions/{rows[0]['revision_id']}").json()
    assert [h["status"] for h in rev["history"]] == ["active"] and rev["scans"] == []
    final = next(d for d in rev["documents"] if d["status_at_print"] == "active")
    assert admin.get(f"/api/documents/{final['id']}/pdf").status_code == 404  # tests make Word files only
    assert admin.get(f"/api/documents/{final['id']}/docx").status_code == 200
    assert admin.get(f"/api/photo-files/{draft_photo}").status_code == 200   # photo on the Active revision
    assert admin.get(f"/api/photo-files/{draft_only}").status_code == 403    # photo only on the draft


# ------------------------------------------------------------------ flags, periodic reviews, copies

def test_flag_and_periodic_review(admin):
    rid = new_procedure(admin)
    to_active(admin, rid, effective="2025-01-10")  # review date 2026-01-10 -> overdue today (2026+)
    dash = admin.get("/api/dashboard").json()
    assert any(r["revision_id"] == rid for r in dash["overdue"])
    assert act(admin, rid, "flag", reason="").status_code == 400
    act(admin, rid, "flag", reason="New air drop added")
    assert admin.get("/api/dashboard").json()["flagged"][0]["flag_reason"] == "New air drop added"
    assert act(admin, rid, "periodic_review", inspected_date=dt.date.today().isoformat(), inspector="").status_code == 400
    rev = act(admin, rid, "periodic_review", inspected_date=dt.date.today().isoformat(),
              inspector="Pat (maintenance lead)").json()["revision"]
    assert rev["next_review_due"] == (dt.date.today().replace(year=dt.date.today().year + 1)).isoformat()
    dash = admin.get("/api/dashboard").json()
    assert not dash["overdue"]
    act(admin, rid, "clear_flag")
    assert admin.get("/api/dashboard").json()["flagged"] == []


def test_copies_listed_when_source_is_revised(admin):
    src = new_procedure(admin)
    to_active(admin, src)
    created = admin.post(f"/api/revisions/{src}/copy", json={"targets": [
        {"asset_number": "10046", "machine_type": "Die Cutter", "department_id": prod_dept(admin)}]}, headers=H).json()
    copy_rid = created[0]["revision_id"]
    assert admin.get(f"/api/revisions/{copy_rid}").json()["source_newer"] is None
    v1 = act(admin, src, "new_revision", reason="change").json()["revision"]
    admin.put(f"/api/revisions/{v1['id']}", json={"version": v1["version"], "effective_date": "2027-01-01"}, headers=H)
    admin.post(f"/api/revisions/{v1['id']}/status", json={"status": "ready_for_review"}, headers=H)
    act(admin, v1["id"], "print_for_review")
    act(admin, v1["id"], "approve", approved_by="Y", approved_date=dt.date.today().isoformat())
    act(admin, v1["id"], "activate")
    assert admin.get(f"/api/revisions/{copy_rid}").json()["source_newer"] == "02-PROD-10045-V01"
    assert admin.get("/api/dashboard").json()["source_revised"][0]["source"] == "02-PROD-10045-V01"
    act(admin, copy_rid, "ack_source_change")
    assert admin.get(f"/api/revisions/{copy_rid}").json()["source_newer"] is None
    assert admin.get("/api/dashboard").json()["source_revised"] == []


def test_dashboard_in_progress_and_no_procedure(admin):
    rid = new_procedure(admin)
    admin.post("/api/assets", json={"asset_number": "20001", "machine_type": "Baler",
                                    "department_id": prod_dept(admin)}, headers=H)
    dash = admin.get("/api/dashboard").json()
    assert [r["revision_id"] for r in dash["in_progress"]["draft"]] == [rid]
    assert [a["asset_number"] for a in dash["no_procedure"]] == ["20001"]


# ------------------------------------------------------------------ scans

def test_scan_upload_and_validation(admin):
    rid = new_procedure(admin)
    pdf = b"%PDF-1.4\n%sample\n"
    r = admin.post(f"/api/revisions/{rid}/scans", data={"kind": "markup", "note": "page 2 notes"},
                   files={"file": ("scan.pdf", pdf, "application/pdf")}, headers=H)
    assert r.status_code == 200, r.text
    img = admin.post(f"/api/revisions/{rid}/scans", data={"kind": "approval"},
                     files={"file": ("signed.jpg", jpeg(), "image/jpeg")}, headers=H)
    assert img.status_code == 200
    bad = admin.post(f"/api/revisions/{rid}/scans", data={"kind": "markup"},
                     files={"file": ("x.exe", b"MZ\x90\x00", "application/octet-stream")}, headers=H)
    assert bad.status_code == 400
    assert admin.post(f"/api/revisions/{rid}/scans", data={"kind": "selfie"},
                      files={"file": ("s.pdf", pdf, "application/pdf")}, headers=H).status_code == 400
    scans = admin.get(f"/api/revisions/{rid}").json()["scans"]
    assert [s["kind"] for s in scans] == ["approval", "markup"]
    got = admin.get(f"/api/scans/{scans[1]['id']}")
    assert got.status_code == 200 and got.content == pdf and got.headers["content-type"] == "application/pdf"


def test_history_events(admin):
    rid = new_procedure(admin)
    to_printed(admin, rid)
    events = admin.get(f"/api/revisions/{rid}").json()["events"]
    actions = [e["action"] for e in events]
    assert "status" in actions and "generate" in actions
