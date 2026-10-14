import io
import json

import pytest
from PIL import Image

from lotowriter.photos import draw_annotations
from test_web import H, client, jpeg, new_procedure, prod_dept, sign_in  # noqa: F401  (fixtures)

DEV_A = {**H, "X-Device-Id": "device-aaaa-1111", "X-Device-Name": "Tablet A"}
DEV_B = {**H, "X-Device-Id": "device-bbbb-2222", "X-Device-Name": "PC B"}


@pytest.fixture()
def admin(client):
    sign_in(client)
    client.post("/api/me/password", json={"current": "Password1", "new": "Villa-Rica-2026"}, headers=H)
    return client


def add_user(c, username, role="full"):
    c.post("/api/admin/users", json={"username": username, "role": role, "temp_password": "Temp-pass-1"}, headers=H)


def become(c, username):
    c.post("/api/logout", headers=H)
    sign_in(c, username, "Temp-pass-1")
    c.post("/api/me/password", json={"current": "Temp-pass-1", "new": f"{username}-pass-22"}, headers=H)


def photo_upload(c, rid, point, temp_id, headers=DEV_A, annotations="[]"):
    return c.post(f"/api/revisions/{rid}/sync-photo", headers=headers,
                  data={"point": str(point), "temp_id": temp_id, "annotations": annotations},
                  files={"file": ("p.jpg", jpeg(), "image/jpeg")})


# ------------------------------------------------------------------ checkout / locking

def test_checkout_blocks_other_devices(admin):
    rid = new_procedure(admin)
    r = admin.post(f"/api/revisions/{rid}/checkout", json={"device_name": "Tablet A"}, headers=DEV_A)
    assert r.status_code == 200 and r.json()["checkout"]["mine"] is True
    # Same user, other device: blocked until it takes over.
    view = admin.get(f"/api/revisions/{rid}", headers=DEV_B).json()
    assert view["checkout"]["mine"] is False and view["checkout"]["device_name"] == "Tablet A"
    v = view["version"]
    blocked = admin.put(f"/api/revisions/{rid}", json={"version": v, "document_owner": "X"}, headers=DEV_B)
    assert blocked.status_code == 409 and "checked out" in blocked.json()["detail"]
    assert admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_B).status_code == 409
    took = admin.post(f"/api/revisions/{rid}/checkout", json={"take_over": True}, headers=DEV_B)
    assert took.status_code == 200 and took.json()["checkout"]["mine"] is True


def test_other_user_cannot_take_over_but_admin_can(admin):
    rid = new_procedure(admin)
    add_user(admin, "author")
    become(admin, "author")
    admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_A)
    # A second Full-access user can't take it over or force-release it.
    admin.post("/api/logout", headers=H)
    sign_in(admin, "admin", "Villa-Rica-2026")
    add_user(admin, "second")
    become(admin, "second")
    r = admin.post(f"/api/revisions/{rid}/checkout", json={"take_over": True}, headers=DEV_B)
    assert r.status_code == 409
    assert admin.post(f"/api/revisions/{rid}/release", json={"force": True}, headers=DEV_B).status_code == 403
    admin.post("/api/logout", headers=H)
    sign_in(admin, "admin", "Villa-Rica-2026")
    assert admin.post(f"/api/revisions/{rid}/release", json={"force": True}, headers=DEV_B).status_code == 200
    assert admin.get(f"/api/revisions/{rid}", headers=DEV_B).json()["checkout"] is None


def test_ready_for_review_releases_checkout(admin):
    rid = new_procedure(admin)
    admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_A)
    ops = [
        {"op_id": "1", "type": "revision.update", "fields": {"effective_date": "2026-11-01", "document_owner": "O"}},
        {"op_id": "2", "type": "point.add", "temp_id": "t-p1", "fields": {"energy_type": "electrical"}},
        {"op_id": "3", "type": "point.update", "point": "t-p1",
         "fields": {"magnitude": "480 VAC", "device": "DS-1", "verify": "Test"}},
    ]
    admin.post(f"/api/revisions/{rid}/sync", json={"ops": ops}, headers=DEV_A)
    assert photo_upload(admin, rid, "t-p1", "t-ph1").status_code == 200
    r = admin.post(f"/api/revisions/{rid}/status", json={"status": "ready_for_review"}, headers=DEV_A)
    assert r.status_code == 200, r.text
    assert r.json()["checkout"] is None


# ------------------------------------------------------------------ sync

def test_sync_with_temp_ids_is_retry_safe(admin):
    rid = new_procedure(admin)
    admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_A)
    ops = [
        {"op_id": "a", "type": "point.add", "temp_id": "t-1", "fields": {"energy_type": "electrical"}},
        {"op_id": "b", "type": "point.update", "point": "t-1", "fields": {"magnitude": "480 VAC", "device": "Main"}},
        {"op_id": "c", "type": "point.add", "temp_id": "t-2", "fields": {"energy_type": "pneumatic"}},
        {"op_id": "d", "type": "point.reorder", "ids": ["t-2", "t-1"]},
        {"op_id": "e", "type": "revision.update", "fields": {"tasks_covered": ["Die change"]}},
    ]
    first = admin.post(f"/api/revisions/{rid}/sync", json={"ops": ops}, headers=DEV_A).json()
    assert all(r["ok"] for r in first["results"]), first["results"]
    # Connection dropped before the device saw the reply -> it sends everything again.
    again = admin.post(f"/api/revisions/{rid}/sync", json={"ops": ops}, headers=DEV_A).json()
    assert all(r["ok"] for r in again["results"])
    rev = again["revision"]
    assert [p["label"] for p in rev["points"]] == ["P-1", "E-1"]
    assert rev["points"][1]["magnitude"] == "480 VAC"
    assert rev["tasks_covered"] == ["Die change"]

    up1 = photo_upload(admin, rid, "t-1", "t-photo-1", annotations=json.dumps(
        [{"type": "arrow", "x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5, "color": "red"}]))
    up2 = photo_upload(admin, rid, "t-1", "t-photo-1")  # retried upload
    assert up1.json()["id"] == up2.json()["id"]
    more = [{"op_id": "f", "type": "photo.annotate", "photo": "t-photo-1",
             "annotations": [{"type": "text", "x": 0.2, "y": 0.8, "text": "E-1", "color": "yellow"}]}]
    assert admin.post(f"/api/revisions/{rid}/sync", json={"ops": more}, headers=DEV_A).json()["results"][0]["ok"]
    photos = admin.get(f"/api/revisions/{rid}", headers=DEV_A).json()["points"][1]["photos"]
    assert len(photos) == 1 and photos[0]["annotations"][0]["text"] == "E-1"


def test_sync_reports_bad_change_and_continues(admin):
    rid = new_procedure(admin)
    admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_A)
    ops = [
        {"op_id": "1", "type": "point.add", "temp_id": "t-1", "fields": {"energy_type": "plasma"}},
        {"op_id": "2", "type": "point.add", "temp_id": "t-2", "fields": {"energy_type": "thermal"}},
        {"op_id": "3", "type": "point.update", "point": "t-1", "fields": {"magnitude": "x"}},
    ]
    res = admin.post(f"/api/revisions/{rid}/sync", json={"ops": ops}, headers=DEV_A).json()["results"]
    assert [r["ok"] for r in res] == [False, True, False]
    assert "energy type" in res[0]["error"]


def test_sync_after_take_over_is_refused(admin):
    rid = new_procedure(admin)
    admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_A)
    admin.post(f"/api/revisions/{rid}/checkout", json={"take_over": True}, headers=DEV_B)
    r = admin.post(f"/api/revisions/{rid}/sync", json={"ops": [
        {"op_id": "1", "type": "revision.update", "fields": {"document_owner": "late"}}]}, headers=DEV_A)
    assert r.status_code == 409 and "could not be uploaded" in r.json()["detail"]
    assert photo_upload(admin, rid, 1, "t-x").status_code == 409


def test_sync_retakes_expired_checkout(admin, tmp_path):
    rid = new_procedure(admin)
    admin.post(f"/api/revisions/{rid}/checkout", json={}, headers=DEV_A)
    from lotowriter.db import connect
    conn = connect()
    conn.execute("UPDATE revisions SET checkout_expires = '2000-01-01 00:00:00' WHERE id = ?", (rid,))
    conn.close()
    r = admin.post(f"/api/revisions/{rid}/sync", json={"ops": [
        {"op_id": "1", "type": "revision.update", "fields": {"document_owner": "renewed"}}]}, headers=DEV_A)
    assert r.status_code == 200 and r.json()["revision"]["checkout"]["mine"] is True


def test_online_edits_still_work_without_checkout(admin):
    rid = new_procedure(admin)
    v = admin.get(f"/api/revisions/{rid}", headers=DEV_B).json()["version"]
    assert admin.put(f"/api/revisions/{rid}", json={"version": v, "document_owner": "PC"},
                     headers=DEV_B).status_code == 200


# ------------------------------------------------------------------ markup

def test_markup_validation(admin):
    rid = new_procedure(admin)
    pid = admin.post(f"/api/revisions/{rid}/points", json={"energy_type": "electrical"}, headers=H).json()["id"]
    phid = admin.post(f"/api/points/{pid}/photos", files={"file": ("a.jpg", jpeg(), "image/jpeg")}, headers=H).json()["id"]
    ok = admin.put(f"/api/photos/{phid}/annotations", json={"annotations": [
        {"type": "ellipse", "x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3, "color": "yellow"}]}, headers=H)
    assert ok.status_code == 200
    for bad in ([{"type": "smiley"}], [{"type": "arrow", "x1": 9, "y1": 0, "x2": 0, "y2": 0}],
                [{"type": "rect", "x": 0, "y": 0, "w": 1, "h": 1, "color": "pink"}]):
        assert admin.put(f"/api/photos/{phid}/annotations", json={"annotations": bad}, headers=H).status_code == 400
    doc = admin.post(f"/api/revisions/{rid}/generate", json={"pdf": False}, headers=H)
    assert doc.status_code == 200


def test_markup_is_drawn_on_print_copy():
    im = Image.new("RGB", (400, 300), "white")
    draw_annotations(im, [{"type": "rect", "x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5, "color": "red"}])
    assert im.getpixel((100, 150))[0] > 200 and im.getpixel((100, 150))[1] < 60  # red edge at x = 0.25
    assert im.getpixel((200, 150)) == (255, 255, 255)  # inside untouched


# ------------------------------------------------------------------ app shell

def test_service_worker_manifest_and_health(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert sw.headers["service-worker-allowed"] == "/"
    m = client.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["start_url"] == "/"
    assert client.get("/api/health").json() == {"ok": True}
    for f in ("app.js", "data.js", "idb.js", "annotate.js", "icons/icon-192.png", "icons/icon-512.png"):
        assert client.get(f"/static/{f}").status_code == 200, f
