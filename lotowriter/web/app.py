"""HTTP API + static web app."""
import json
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import auth, service, workflow
from ..db import connect, migrate
from ..energy import load_energy_types
from ..generate import STATUS_LABELS

STATIC = Path(__file__).resolve().parent / "static"
mimetypes.add_type("application/manifest+json", ".webmanifest")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
COOKIE = "lw_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "LotoWriter"
SECURE_COOKIE = os.environ.get("LOTOWRITER_SECURE_COOKIE", "1") != "0"


@asynccontextmanager
async def lifespan(_app):
    conn = connect()
    migrate(conn)
    conn.close()
    yield


app = FastAPI(title="LotoWriter", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ------------------------------------------------------------------ plumbing

def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.middleware("http")
async def guard(request: Request, call_next):
    # Mutating API calls must come from our own page (custom header can't be sent cross-site without CORS).
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate (ETag) so updates show up immediately
    return response


@app.exception_handler(service.ServiceError)
def _service_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


def _user(request: Request, conn=Depends(db), allow_password_change=False):
    row = auth.user_for_token(conn, request.cookies.get(COOKIE))
    if not row:
        raise HTTPException(401, "Please sign in.")
    if row["must_change_password"] and not allow_password_change:
        raise HTTPException(403, "You must change your password first.")
    user = dict(row)
    device = request.headers.get("x-device-id", "")
    user["device_id"] = device if _DEVICE_RE.match(device) else ""
    user["device_name"] = request.headers.get("x-device-name", "")[:60]
    return user


def user_any(request: Request, conn=Depends(db)):
    """Signed in, even if a password change is pending."""
    return _user(request, conn, allow_password_change=True)


def user_ok(request: Request, conn=Depends(db)):
    return _user(request, conn)


def user_admin(user=Depends(user_ok)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only.")
    return user


def _me(user):
    return {k: user[k] for k in ("id", "username", "display_name", "role", "must_change_password")}


# ------------------------------------------------------------------ auth

@app.post("/api/login")
def login(body: dict, response: Response, conn=Depends(db)):
    try:
        user, token = auth.login(conn, str(body.get("username", "")), str(body.get("password", "")))
    except auth.LoginError as e:
        raise HTTPException(401, str(e))
    response.set_cookie(COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict",
                        secure=SECURE_COOKIE, path="/")
    return _me(user)


@app.post("/api/logout")
def logout(request: Request, response: Response, conn=Depends(db)):
    auth.logout(conn, request.cookies.get(COOKIE) or "")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(user_any)):
    return _me(user)


@app.post("/api/me/password")
def change_password(body: dict, request: Request, user=Depends(user_any), conn=Depends(db)):
    if not auth.verify_password(str(body.get("current", "")), user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    new = str(body.get("new", ""))
    problem = auth.password_problem(new, user["username"])
    if problem:
        raise HTTPException(400, problem)
    if auth.verify_password(new, user["password_hash"]):
        raise HTTPException(400, "New password must be different from the current one.")
    auth.set_password(conn, user["id"], new, must_change=False)
    auth.end_sessions(conn, user["id"], keep_token=request.cookies.get(COOKIE))
    service.audit(conn, user, "user", user["id"], "change_password")
    return {"ok": True}


# ------------------------------------------------------------------ reference data

@app.get("/api/config")
def config(user=Depends(user_ok), conn=Depends(db)):
    return {
        "energy_types": [t.__dict__ | {"units": list(t.units)} for t in load_energy_types().values()],
        "sites": service.sites(conn),
        "departments": service.departments(conn),
        "status_labels": STATUS_LABELS,
    }


@app.post("/api/admin/sites")
def add_site(body: dict, user=Depends(user_admin), conn=Depends(db)):
    return {"id": service.add_site(conn, user, body.get("code"), body.get("name"))}


@app.post("/api/admin/departments")
def add_department(body: dict, user=Depends(user_admin), conn=Depends(db)):
    return {"id": service.add_department(conn, user, body.get("site_id"), body.get("code"), body.get("name"))}


# ------------------------------------------------------------------ assets & procedures

@app.get("/api/assets")
def list_assets(without_procedure: int = 0, user=Depends(user_ok), conn=Depends(db)):
    return service.assets(conn, without_procedure=bool(without_procedure))


@app.post("/api/assets")
def create_asset(body: dict, user=Depends(user_ok), conn=Depends(db)):
    return {"id": service.create_asset(conn, user, body)}


@app.put("/api/assets/{asset_id}")
def update_asset(asset_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.update_asset(conn, user, asset_id, body)
    return {"ok": True}


@app.get("/api/procedures")
def list_procedures(q: str = "", department_id: int | None = None, status: str | None = None,
                    user=Depends(user_ok), conn=Depends(db)):
    return service.list_procedures(conn, user, q.strip(), department_id, status or None)


@app.post("/api/procedures")
def create_procedure(body: dict, user=Depends(user_ok), conn=Depends(db)):
    """Body: {"asset_id": n} for an existing asset, or {"asset": {...}} to create the asset too."""
    service.require_editor(user)
    if body.get("asset"):
        with service.tx(conn):
            asset_id = service.create_asset(conn, user, body["asset"], _in_tx=True)
            pid, rid = service.create_procedure(conn, user, asset_id, _in_tx=True)
    else:
        pid, rid = service.create_procedure(conn, user, int(body.get("asset_id") or 0))
    return {"procedure_id": pid, "revision_id": rid}


@app.get("/api/revisions/{revision_id}")
def get_revision(revision_id: int, user=Depends(user_ok), conn=Depends(db)):
    return workflow.full_revision(conn, user, revision_id)


@app.post("/api/revisions/{revision_id}/workflow")
def workflow_action(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    """Review/approval steps: print_for_review, return, approve, withdraw_approval, activate,
    new_revision, flag, clear_flag, periodic_review, ack_source_change."""
    result = workflow.transition(conn, user, revision_id, body.get("action"), body)
    target = result.get("revision_id", revision_id) if isinstance(result, dict) else revision_id
    return {"result": result, "revision": workflow.full_revision(conn, user, target)}


@app.post("/api/revisions/{revision_id}/scans")
async def upload_scan(revision_id: int, kind: str = Form(...), note: str = Form(""), file: UploadFile = File(...),
                      user=Depends(user_ok), conn=Depends(db)):
    content = await file.read(workflow.MAX_SCAN_BYTES + 1)
    return {"id": workflow.add_scan(conn, user, revision_id, kind, content, file.filename or "", note)}


@app.get("/api/scans/{scan_id}")
def get_scan(scan_id: int, user=Depends(user_ok), conn=Depends(db)):
    path, ctype, name = workflow.scan_file(conn, user, scan_id)
    return FileResponse(path, media_type=ctype, filename=name, content_disposition_type="inline")


@app.get("/api/dashboard")
def dashboard(user=Depends(user_ok), conn=Depends(db)):
    return workflow.dashboard(conn, user)


@app.put("/api/revisions/{revision_id}")
def update_revision(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.update_revision(conn, user, revision_id, body, body.get("version"))
    return service.get_revision(conn, user, revision_id)


@app.post("/api/revisions/{revision_id}/points")
def add_point(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    return {"id": service.add_point(conn, user, revision_id, body)}


@app.put("/api/revisions/{revision_id}/point-order")
def reorder(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.reorder_points(conn, user, revision_id, body.get("ids") or [], body.get("version"))
    return service.get_revision(conn, user, revision_id)


@app.get("/api/revisions/{revision_id}/check")
def check(revision_id: int, user=Depends(user_ok), conn=Depends(db)):
    return service.completeness(conn, user, revision_id)


@app.post("/api/revisions/{revision_id}/status")
def set_status(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.set_status(conn, user, revision_id, body.get("status"))
    return service.get_revision(conn, user, revision_id)


@app.post("/api/revisions/{revision_id}/generate")
def generate_docs(revision_id: int, body: dict | None = None, user=Depends(user_ok), conn=Depends(db)):
    return service.generate_documents(conn, user, revision_id, pdf=(body or {}).get("pdf", True))


@app.get("/api/documents/{document_id}/{kind}")
def download(document_id: int, kind: str, user=Depends(user_ok), conn=Depends(db)):
    if kind not in ("pdf", "docx"):
        raise HTTPException(404)
    path = service.document_file(conn, user, document_id, kind)
    media = "application/pdf" if kind == "pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    # PDF opens in the browser for printing; DOCX downloads.
    return FileResponse(path, media_type=media, filename=path.name,
                        content_disposition_type="inline" if kind == "pdf" else "attachment")


@app.post("/api/revisions/{revision_id}/copy")
def copy(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    return service.copy_procedure(conn, user, revision_id, body.get("targets") or [], body.get("include"))


# ------------------------------------------------------------------ points & photos

@app.put("/api/points/{point_id}")
def update_point(point_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.update_point(conn, user, point_id, body, body.get("version"))
    return {"ok": True}


@app.delete("/api/points/{point_id}")
def delete_point(point_id: int, user=Depends(user_ok), conn=Depends(db)):
    service.delete_point(conn, user, point_id)
    return {"ok": True}


@app.post("/api/points/{point_id}/check")
def check_point(point_id: int, user=Depends(user_ok), conn=Depends(db)):
    service.mark_point_checked(conn, user, point_id)
    return {"ok": True}


@app.post("/api/points/{point_id}/photos")
async def upload_photo(point_id: int, file: UploadFile = File(...), user=Depends(user_ok), conn=Depends(db)):
    content = await file.read(service.MAX_PHOTO_BYTES + 1)
    return {"id": service.add_photo(conn, user, point_id, content, file.filename or "")}


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, user=Depends(user_ok), conn=Depends(db)):
    service.delete_photo(conn, user, photo_id)
    return {"ok": True}


@app.post("/api/photos/{photo_id}/move")
def move_photo(photo_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.move_photo(conn, user, photo_id, body.get("direction"))
    return {"ok": True}


@app.put("/api/photos/{photo_id}/annotations")
def annotate_photo(photo_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    service.set_photo_annotations(conn, user, photo_id, body.get("annotations") or [])
    return {"ok": True}


# ------------------------------------------------------------------ offline: checkout & sync

@app.post("/api/revisions/{revision_id}/checkout")
def checkout(revision_id: int, body: dict | None = None, user=Depends(user_ok), conn=Depends(db)):
    body = body or {}
    service.checkout(conn, user, revision_id, device_name=body.get("device_name") or user["device_name"],
                     take_over=bool(body.get("take_over")))
    return service.get_revision(conn, user, revision_id)


@app.post("/api/revisions/{revision_id}/release")
def release(revision_id: int, body: dict | None = None, user=Depends(user_ok), conn=Depends(db)):
    service.release(conn, user, revision_id, force=bool((body or {}).get("force")))
    return {"ok": True}


@app.post("/api/revisions/{revision_id}/sync")
def sync(revision_id: int, body: dict, user=Depends(user_ok), conn=Depends(db)):
    results = service.sync(conn, user, revision_id, body.get("ops") or [])
    return {"results": results, "revision": service.get_revision(conn, user, revision_id)}


@app.post("/api/revisions/{revision_id}/sync-photo")
async def sync_photo(revision_id: int, point: str = Form(...), temp_id: str = Form(...),
                     annotations: str = Form("[]"), file: UploadFile = File(...),
                     user=Depends(user_ok), conn=Depends(db)):
    content = await file.read(service.MAX_PHOTO_BYTES + 1)
    try:
        shapes = json.loads(annotations or "[]")
    except ValueError:
        raise HTTPException(400, "Bad photo markup.")
    pid = service.sync_photo(conn, user, revision_id, point, content, file.filename or "", temp_id, shapes)
    return {"id": pid}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/photo-files/{file_id}")
def photo_file(file_id: str, user=Depends(user_ok), conn=Depends(db)):
    if user["role"] == "read_only" and not workflow.photo_visible_to_read_only(conn, file_id):
        raise HTTPException(403, "Read-only accounts can only view photos of Active procedures.")
    return FileResponse(service.photo_path(file_id), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})  # file ids never change


# ------------------------------------------------------------------ users (admin)

@app.get("/api/admin/users")
def users(user=Depends(user_admin), conn=Depends(db)):
    return service.list_users(conn)


@app.post("/api/admin/users")
def create_user(body: dict, user=Depends(user_admin), conn=Depends(db)):
    return {"id": service.create_user(conn, user, body.get("username"), body.get("display_name"),
                                      body.get("role"), str(body.get("temp_password", "")))}


@app.put("/api/admin/users/{user_id}")
def update_user(user_id: int, body: dict, user=Depends(user_admin), conn=Depends(db)):
    service.update_user(conn, user, user_id, body)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-password")
def reset_password(user_id: int, body: dict, user=Depends(user_admin), conn=Depends(db)):
    service.reset_password(conn, user, user_id, str(body.get("temp_password", "")))
    return {"ok": True}


# ------------------------------------------------------------------ web app

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker():
    # Served from the root so it can control the whole app.
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
