import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

H = {"X-Requested-With": "LotoWriter"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOTOWRITER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("LOTOWRITER_SECURE_COOKIE", "0")
    monkeypatch.setenv("LOTOWRITER_PDF", "0")
    import importlib
    import lotowriter.web.app as appmod
    importlib.reload(appmod)  # pick up SECURE_COOKIE
    with TestClient(appmod.app) as c:
        yield c


def sign_in(c, username="admin", password="Password1"):
    return c.post("/api/login", json={"username": username, "password": password}, headers=H)


@pytest.fixture()
def admin(client):
    sign_in(client)
    r = client.post("/api/me/password", json={"current": "Password1", "new": "Villa-Rica-2026"}, headers=H)
    assert r.status_code == 200, r.text
    return client


def jpeg(w=800, h=600, color="red"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def prod_dept(c):
    return next(d for d in c.get("/api/config").json()["departments"] if d["code"] == "PROD")["id"]


def new_procedure(c, number="10045", machine="Die Cutter"):
    r = c.post("/api/procedures", json={"asset": {"asset_number": number, "machine_type": machine,
                                                  "department_id": prod_dept(c), "oem": "OEM", "model": "M1"}},
               headers=H)
    assert r.status_code == 200, r.text
    return r.json()["revision_id"]


def fill_ready(c, rid):
    rev = c.get(f"/api/revisions/{rid}").json()
    c.put(f"/api/revisions/{rid}", json={"version": rev["version"], "effective_date": "2026-11-01",
                                         "document_owner": "Owner"}, headers=H)
    pid = c.post(f"/api/revisions/{rid}/points", json={"energy_type": "electrical", "magnitude": "480 VAC",
                                                       "device": "Disconnect DS-10045 on panel MCC-2",
                                                       "verify": "Test for absence of voltage"},
                 headers=H).json()["id"]
    r = c.post(f"/api/points/{pid}/photos", files={"file": ("a.jpg", jpeg(), "image/jpeg")}, headers=H)
    assert r.status_code == 200, r.text
    return pid


# ------------------------------------------------------------------ accounts

def test_default_admin_must_change_password(client):
    assert sign_in(client).json()["must_change_password"] == 1
    assert client.get("/api/procedures").status_code == 403
    bad = client.post("/api/me/password", json={"current": "Password1", "new": "Password1"}, headers=H)
    assert bad.status_code == 400
    ok = client.post("/api/me/password", json={"current": "Password1", "new": "Villa-Rica-2026"}, headers=H)
    assert ok.status_code == 200
    assert client.get("/api/procedures").status_code == 200


def test_mutations_need_csrf_header(admin):
    assert admin.post("/api/procedures", json={}).status_code == 403


def test_not_signed_in(client):
    assert client.get("/api/procedures").status_code == 401


def test_lockout_after_ten_failures(client):
    for _ in range(10):
        assert sign_in(client, password="wrong").status_code == 401
    r = sign_in(client)  # correct password, but locked
    assert r.status_code == 401 and "Too many" in r.json()["detail"]


def test_last_admin_cannot_be_removed(admin):
    me = admin.get("/api/me").json()
    r = admin.put(f"/api/admin/users/{me['id']}", json={"role": "full"}, headers=H)
    assert r.status_code == 409


def test_read_only_user(admin):
    rid = new_procedure(admin)
    admin.post("/api/admin/users", json={"username": "viewer", "display_name": "V", "role": "read_only",
                                         "temp_password": "Temp-pass-1"}, headers=H)
    admin.post("/api/logout", headers=H)
    sign_in(admin, "viewer", "Temp-pass-1")
    admin.post("/api/me/password", json={"current": "Temp-pass-1", "new": "Viewer-pass-2"}, headers=H)
    assert admin.get("/api/procedures").json() == []  # drafts hidden
    assert admin.get(f"/api/revisions/{rid}").status_code == 403
    r = admin.post("/api/procedures", json={"asset": {"asset_number": "1", "machine_type": "x",
                                                      "department_id": prod_dept(admin)}}, headers=H)
    assert r.status_code == 403


# ------------------------------------------------------------------ procedures

def test_create_procedure_numbering_and_one_per_asset(admin):
    rid = new_procedure(admin)
    rev = admin.get(f"/api/revisions/{rid}").json()
    assert rev["procedure_number"] == "02-PROD-10045-V00"
    assert rev["status"] == "draft"
    again = admin.post("/api/procedures", json={"asset_id": rev["asset_id"]}, headers=H)
    assert again.status_code == 409
    dup = admin.post("/api/assets", json={"asset_number": "10045", "machine_type": "x",
                                          "department_id": prod_dept(admin)}, headers=H)
    assert dup.status_code == 409


def test_number_stays_when_asset_changes_department(admin):
    rid = new_procedure(admin)
    rev = admin.get(f"/api/revisions/{rid}").json()
    wh = next(d for d in admin.get("/api/config").json()["departments"] if d["code"] == "WH")["id"]
    admin.put(f"/api/assets/{rev['asset_id']}", json={"asset_number": "10045", "machine_type": "Die Cutter",
                                                     "department_id": wh}, headers=H)
    rev = admin.get(f"/api/revisions/{rid}").json()
    assert rev["procedure_number"] == "02-PROD-10045-V00" and rev["dept_code"] == "WH"


def test_review_date_default_and_manual(admin):
    rid = new_procedure(admin)
    v = admin.get(f"/api/revisions/{rid}").json()["version"]
    rev = admin.put(f"/api/revisions/{rid}", json={"version": v, "effective_date": "2026-11-01"}, headers=H).json()
    assert rev["review_date"] == "2027-11-01"
    rev = admin.put(f"/api/revisions/{rid}", json={"version": rev["version"], "review_date": "2027-06-01",
                                                   "review_date_manual": True}, headers=H).json()
    rev = admin.put(f"/api/revisions/{rid}", json={"version": rev["version"], "effective_date": "2026-12-01"},
                    headers=H).json()
    assert rev["review_date"] == "2027-06-01"  # manual date kept
    rev = admin.put(f"/api/revisions/{rid}", json={"version": rev["version"], "review_date_manual": False},
                    headers=H).json()
    assert rev["review_date"] == "2027-12-01"


def test_stale_version_conflict(admin):
    rid = new_procedure(admin)
    v = admin.get(f"/api/revisions/{rid}").json()["version"]
    assert admin.put(f"/api/revisions/{rid}", json={"version": v, "document_owner": "A"}, headers=H).status_code == 200
    assert admin.put(f"/api/revisions/{rid}", json={"version": v, "document_owner": "B"}, headers=H).status_code == 409


def test_points_labels_order_and_photos(admin):
    rid = new_procedure(admin)
    ids = [admin.post(f"/api/revisions/{rid}/points", json={"energy_type": t}, headers=H).json()["id"]
           for t in ("electrical", "pneumatic", "electrical", "startstop")]
    rev = admin.get(f"/api/revisions/{rid}").json()
    assert [p["label"] for p in rev["points"]] == ["E-1", "P-1", "E-2", "SS-1"]
    assert rev["points"][3]["locks"] == 0
    new_order = [ids[1], ids[0], ids[2], ids[3]]
    rev = admin.put(f"/api/revisions/{rid}/point-order", json={"ids": new_order, "version": rev["version"]},
                    headers=H).json()
    assert [p["label"] for p in rev["points"]] == ["P-1", "E-1", "E-2", "SS-1"]
    # Sideways phone photo comes out upright; non-images rejected.
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (400, 300)).save(buf, "JPEG", exif=exif)
    assert admin.post(f"/api/points/{ids[0]}/photos", files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")},
                      headers=H).status_code == 200
    ph = admin.get(f"/api/revisions/{rid}").json()["points"][1]["photos"][0]
    assert ph["width"] < ph["height"]
    assert admin.get(f"/api/photo-files/{ph['file_id']}").status_code == 200
    bad = admin.post(f"/api/points/{ids[0]}/photos", files={"file": ("x.jpg", b"not an image", "image/jpeg")},
                     headers=H)
    assert bad.status_code == 400


def test_ready_for_review_gate_and_edit_returns_to_draft(admin):
    rid = new_procedure(admin)
    blocked = admin.post(f"/api/revisions/{rid}/status", json={"status": "ready_for_review"}, headers=H)
    assert blocked.status_code == 400 and "No isolation points" in blocked.json()["detail"]
    pid = fill_ready(admin, rid)
    check = admin.get(f"/api/revisions/{rid}/check").json()
    assert check["ready"], check
    rev = admin.post(f"/api/revisions/{rid}/status", json={"status": "ready_for_review"}, headers=H).json()
    assert rev["status"] == "ready_for_review"
    admin.put(f"/api/points/{pid}", json={"magnitude": "480 VAC 3-phase"}, headers=H)
    assert admin.get(f"/api/revisions/{rid}").json()["status"] == "draft"


def test_generate_and_download(admin):
    rid = new_procedure(admin)
    fill_ready(admin, rid)
    doc = admin.post(f"/api/revisions/{rid}/generate", json={"pdf": False}, headers=H).json()
    r = admin.get(f"/api/documents/{doc['id']}/docx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert admin.get(f"/api/documents/{doc['id']}/pdf").status_code == 404


# ------------------------------------------------------------------ copy from similar machine

def test_copy_to_similar_machine(admin):
    src = new_procedure(admin)
    fill_ready(admin, src)
    v = admin.get(f"/api/revisions/{src}").json()["version"]
    admin.put(f"/api/revisions/{src}", json={"version": v, "tasks_covered": ["Die change on 10045"]}, headers=H)
    created = admin.post(f"/api/revisions/{src}/copy", json={"targets": [
        {"asset_number": "10046", "machine_type": "Die Cutter", "department_id": prod_dept(admin)},
        {"asset_number": "10047", "machine_type": "Die Cutter", "department_id": prod_dept(admin)},
    ]}, headers=H)
    assert created.status_code == 200, created.text
    numbers = [c["procedure_number"] for c in created.json()]
    assert numbers == ["02-PROD-10046-V00", "02-PROD-10047-V00"]

    rid = created.json()[0]["revision_id"]
    rev = admin.get(f"/api/revisions/{rid}").json()
    p = rev["points"][0]
    assert rev["tasks_covered"] == ["Die change on 10046"]
    assert p["device"] == "Disconnect DS-10046 on panel MCC-2"
    assert p["copy_status"] == "needs_check"
    assert "MCC-2" in p["copy_flags"]["check_tokens"]
    assert p["photos"][0]["copied_from_photo_id"] is not None
    src_rev = admin.get(f"/api/revisions/{src}").json()
    assert p["photos"][0]["file_id"] == src_rev["points"][0]["photos"][0]["file_id"]  # shared file
    assert rev["copied_from"] == "02-PROD-10045-V00"
    assert set(src_rev["copies"]) == set(numbers)

    blocked = admin.post(f"/api/revisions/{rid}/status", json={"status": "ready_for_review"}, headers=H)
    assert blocked.status_code == 400 and "not yet checked" in blocked.json()["detail"]
    assert admin.post(f"/api/points/{p['id']}/check", headers=H).status_code == 200
    rev = admin.get(f"/api/revisions/{rid}").json()
    assert rev["points"][0]["copy_status"] == "confirmed"
    ok = admin.post(f"/api/revisions/{rid}/status", json={"status": "ready_for_review"}, headers=H)
    assert ok.status_code == 200, ok.text


def test_copy_refuses_asset_with_procedure(admin):
    src = new_procedure(admin, "10045")
    other = new_procedure(admin, "10046")
    asset_id = admin.get(f"/api/revisions/{other}").json()["asset_id"]
    r = admin.post(f"/api/revisions/{src}/copy", json={"targets": [{"asset_id": asset_id}]}, headers=H)
    assert r.status_code == 409
    # Nothing half-created.
    assert len(admin.get("/api/procedures").json()) == 2
