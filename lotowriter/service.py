"""Business rules on top of the database. The web layer only calls these."""
import datetime as dt
import io
import json
import os
import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .db import data_dir
from .energy import load_energy_types
from .generate import assess, default_review_date, generate, parse_date

try:  # iPhone HEIC photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover
    pass

EDITABLE_STATUSES = ("draft", "ready_for_review", "returned")
LIST_FIELDS = ("tasks_covered", "affected_employees", "lockout_devices", "restart_steps")
TEXT_FIELDS = ("shutdown_procedure", "isolate_text", "group_lockout", "stored_energy", "verify_isolation",
               "servicing_notes", "locks_override_reason", "document_owner", "change_reason")
POINT_TEXT_FIELDS = ("label", "energy_type", "magnitude", "device", "deenergize", "lock", "stored_energy",
                     "verify", "notes", "photo_missing_reason")
POINT_CHECK_FIELDS = ("device", "deenergize", "lock", "stored_energy", "verify", "notes")
MAX_PHOTO_BYTES = 25 * 1024 * 1024
DISPLAY_LONG_EDGE = 1600
ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


class ServiceError(Exception):
    """A rule was broken; message is safe to show the user."""
    status = 400


class NotFound(ServiceError):
    status = 404


class Conflict(ServiceError):
    status = 409


class Forbidden(ServiceError):
    status = 403


class Locked(Conflict):
    """Checked out to someone else / another device."""


# ------------------------------------------------------------------ helpers

class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK (connection uses autocommit mode)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def audit(conn, user, entity, entity_id, action, detail=""):
    conn.execute("INSERT INTO audit_log(user_id, entity, entity_id, action, detail) VALUES (?, ?, ?, ?, ?)",
                 (user["id"] if user else None, entity, entity_id, action,
                  detail if isinstance(detail, str) else json.dumps(detail)))


def require_editor(user):
    if user["role"] not in ("admin", "full"):
        raise Forbidden("Your account is read-only.")


def _clean(value, max_len=4000):
    return str(value or "").strip()[:max_len]


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ sites / departments

def sites(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM sites ORDER BY code")]


def departments(conn):
    return [dict(r) for r in conn.execute(
        "SELECT d.*, s.code AS site_code FROM departments d JOIN sites s ON s.id = d.site_id ORDER BY s.code, d.code")]


_CODE_RE = re.compile(r"^[A-Z0-9]{2,6}$")


def add_site(conn, user, code, name):
    code = _clean(code).upper()
    if not _CODE_RE.match(code):
        raise ServiceError("Site code must be 2-6 letters/digits.")
    if not _clean(name):
        raise ServiceError("Site name is required.")
    with tx(conn):
        if conn.execute("SELECT 1 FROM sites WHERE code = ?", (code,)).fetchone():
            raise Conflict(f"Site code {code} already exists.")
        cur = conn.execute("INSERT INTO sites(code, name) VALUES (?, ?)", (code, _clean(name, 100)))
        audit(conn, user, "site", cur.lastrowid, "create", code)
    return cur.lastrowid


def add_department(conn, user, site_id, code, name):
    code = _clean(code).upper()
    if not _CODE_RE.match(code):
        raise ServiceError("Department code must be 2-6 letters/digits.")
    if not _clean(name):
        raise ServiceError("Department name is required.")
    with tx(conn):
        if conn.execute("SELECT 1 FROM departments WHERE site_id = ? AND code = ?", (site_id, code)).fetchone():
            raise Conflict(f"Department code {code} already exists at this site.")
        cur = conn.execute("INSERT INTO departments(site_id, code, name) VALUES (?, ?, ?)",
                           (site_id, code, _clean(name, 100)))
        audit(conn, user, "department", cur.lastrowid, "create", code)
    return cur.lastrowid


# ------------------------------------------------------------------ assets

ASSET_FIELDS = ("asset_number", "machine_type", "oem", "model", "serial", "location")


def _asset_values(fields):
    v = {k: _clean(fields.get(k), 200) for k in ASSET_FIELDS}
    if not v["asset_number"]:
        raise ServiceError("Asset # is required.")
    if not v["machine_type"]:
        raise ServiceError("Machine type is required.")
    return v


def _department(conn, department_id):
    d = conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
    if not d:
        raise ServiceError("Choose a department.")
    return d


def create_asset(conn, user, fields, _in_tx=False):
    require_editor(user)
    v = _asset_values(fields)
    dept = _department(conn, fields.get("department_id"))

    def run():
        if conn.execute("SELECT 1 FROM assets WHERE site_id = ? AND asset_number = ? COLLATE NOCASE",
                        (dept["site_id"], v["asset_number"])).fetchone():
            raise Conflict(f"Asset # {v['asset_number']} already exists at this site.")
        cur = conn.execute(
            "INSERT INTO assets(site_id, department_id, asset_number, machine_type, oem, model, serial, location) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (dept["site_id"], dept["id"], *[v[k] for k in ASSET_FIELDS]))
        audit(conn, user, "asset", cur.lastrowid, "create", v["asset_number"])
        return cur.lastrowid

    if _in_tx:
        return run()
    with tx(conn):
        return run()


def update_asset(conn, user, asset_id, fields):
    """Edit asset details. The procedure number stays as first assigned."""
    require_editor(user)
    v = _asset_values(fields)
    dept = _department(conn, fields.get("department_id"))
    with tx(conn):
        old = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not old:
            raise NotFound("Asset not found.")
        dup = conn.execute("SELECT id FROM assets WHERE site_id = ? AND asset_number = ? COLLATE NOCASE AND id != ?",
                           (dept["site_id"], v["asset_number"], asset_id)).fetchone()
        if dup:
            raise Conflict(f"Asset # {v['asset_number']} already exists at this site.")
        conn.execute("UPDATE assets SET department_id = ?, asset_number = ?, machine_type = ?, oem = ?, model = ?, "
                     "serial = ?, location = ? WHERE id = ?",
                     (dept["id"], *[v[k] for k in ASSET_FIELDS], asset_id))
        changes = {k: [old[k], v[k]] for k in ASSET_FIELDS if old[k] != v[k]}
        if old["department_id"] != dept["id"]:
            changes["department_id"] = [old["department_id"], dept["id"]]
        audit(conn, user, "asset", asset_id, "update", changes)


def assets(conn, without_procedure=False):
    sql = ("SELECT a.*, d.code AS dept_code, d.name AS dept_name, s.code AS site_code, s.name AS site_name, "
           "p.id AS procedure_id FROM assets a JOIN departments d ON d.id = a.department_id "
           "JOIN sites s ON s.id = a.site_id LEFT JOIN procedures p ON p.asset_id = a.id "
           "WHERE a.status = 'active'")
    if without_procedure:
        sql += " AND p.id IS NULL"
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.asset_number")]


# ------------------------------------------------------------------ procedures / revisions

def number_base(site_code, dept_code, asset_number):
    return f"{site_code}-{dept_code}-{str(asset_number).strip().replace('-', '_')}"


def create_procedure(conn, user, asset_id, _in_tx=False, copied_from=None):
    """Create the asset's one procedure with revision V00 (Draft). Returns (procedure_id, revision_id)."""
    require_editor(user)

    def run():
        a = conn.execute("SELECT a.*, s.code AS site_code, d.code AS dept_code FROM assets a "
                         "JOIN sites s ON s.id = a.site_id JOIN departments d ON d.id = a.department_id "
                         "WHERE a.id = ?", (asset_id,)).fetchone()
        if not a:
            raise NotFound("Asset not found.")
        existing = conn.execute("SELECT id FROM procedures WHERE asset_id = ?", (asset_id,)).fetchone()
        if existing:
            raise Conflict(f"Asset {a['asset_number']} already has a procedure (one procedure per asset).")
        base = number_base(a["site_code"], a["dept_code"], a["asset_number"])
        if conn.execute("SELECT 1 FROM procedures WHERE number_base = ?", (base,)).fetchone():
            raise Conflict(f"Procedure number {base} is already in use.")
        pid = conn.execute("INSERT INTO procedures(asset_id, number_base, copied_from_revision_id, created_by) "
                           "VALUES (?, ?, ?, ?)", (asset_id, base, copied_from, user["id"])).lastrowid
        rid = conn.execute("INSERT INTO revisions(procedure_id, rev_no, created_by, updated_by) VALUES (?, 0, ?, ?)",
                           (pid, user["id"], user["id"])).lastrowid
        audit(conn, user, "procedure", pid, "create", f"{base}-V00")
        return pid, rid

    if _in_tx:
        return run()
    with tx(conn):
        return run()


def current_revision_id(conn, procedure_id):
    r = conn.execute("SELECT id FROM revisions WHERE procedure_id = ? ORDER BY rev_no DESC LIMIT 1",
                     (procedure_id,)).fetchone()
    if not r:
        raise NotFound("Procedure not found.")
    return r["id"]


def list_procedures(conn, user, q="", department_id=None, status=None):
    sql = """
      SELECT p.id, p.number_base, r.id AS revision_id, r.rev_no, r.status, r.updated_at,
             r.effective_date, r.review_date,
             a.id AS asset_id, a.asset_number, a.machine_type, a.oem, a.model,
             d.id AS department_id, d.code AS dept_code, d.name AS dept_name,
             (SELECT COUNT(*) FROM points pt WHERE pt.revision_id = r.id) AS point_count,
             (SELECT COUNT(*) FROM points pt WHERE pt.revision_id = r.id AND pt.copy_status = 'needs_check')
               AS needs_check,
             (SELECT rev_no FROM revisions ra WHERE ra.procedure_id = p.id AND ra.status = 'active') AS active_rev_no,
             p.flag_reason
      FROM procedures p
      JOIN revisions r ON r.procedure_id = p.id AND {revision_filter}
      JOIN assets a ON a.id = p.asset_id
      JOIN departments d ON d.id = a.department_id
      WHERE 1 = 1"""
    args = []
    # Read-only users see the Active revision (even while a newer draft exists); others see the latest.
    sql = sql.replace("{revision_filter}", "r.status = 'active'" if user["role"] == "read_only" else
                      "r.rev_no = (SELECT MAX(rev_no) FROM revisions WHERE procedure_id = p.id)")
    if q:
        sql += (" AND (a.asset_number LIKE ? OR a.machine_type LIKE ? OR a.oem LIKE ? OR a.model LIKE ?"
                " OR p.number_base LIKE ?)")
        args += [f"%{q}%"] * 5
    if department_id:
        sql += " AND d.id = ?"
        args.append(department_id)
    if status:
        sql += " AND r.status = ?"
        args.append(status)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY a.asset_number", args)]
    for r in rows:
        r["procedure_number"] = f"{r['number_base']}-V{r['rev_no']:02d}"
    return rows


def _point_dict(conn, row):
    p = dict(row)
    p["copy_flags"] = json.loads(p["copy_flags"] or "{}")
    p["photos"] = [dict(ph) for ph in conn.execute(
        "SELECT id, seq, file_id, original_name, width, height, copied_from_photo_id, created_at, annotations "
        "FROM photos WHERE point_id = ? ORDER BY seq", (row["id"],))]
    for ph in p["photos"]:
        ph["annotations"] = json.loads(ph["annotations"] or "[]")
    return p


def get_revision(conn, user, revision_id):
    r = conn.execute("""
        SELECT r.*, p.number_base, p.asset_id, p.copied_from_revision_id, p.id AS procedure_id,
               a.asset_number, a.machine_type, a.oem, a.model, a.serial, a.location, a.department_id,
               d.code AS dept_code, d.name AS dept_name, s.code AS site_code, s.name AS site_name
        FROM revisions r JOIN procedures p ON p.id = r.procedure_id JOIN assets a ON a.id = p.asset_id
        JOIN departments d ON d.id = a.department_id JOIN sites s ON s.id = a.site_id
        WHERE r.id = ?""", (revision_id,)).fetchone()
    if not r:
        raise NotFound("Procedure not found.")
    if user["role"] == "read_only" and r["status"] != "active":
        raise Forbidden("Read-only accounts can only view Active procedures.")
    rev = dict(r)
    for f in LIST_FIELDS:
        rev[f] = json.loads(rev[f] or "[]")
    rev["procedure_number"] = f"{rev['number_base']}-V{rev['rev_no']:02d}"
    rev["points"] = [_point_dict(conn, p) for p in conn.execute(
        "SELECT * FROM points WHERE revision_id = ? ORDER BY seq", (revision_id,))]
    rev["locks_calculated"] = sum(p["locks"] for p in rev["points"])
    rev["editable"] = rev["status"] in EDITABLE_STATUSES and user["role"] != "read_only"
    rev["checkout"] = None
    if _lease_active(r):
        holder = conn.execute("SELECT display_name FROM users WHERE id = ?", (r["checkout_user_id"],)).fetchone()
        rev["checkout"] = {"user_id": r["checkout_user_id"], "user_name": holder["display_name"] if holder else "?",
                           "device_name": r["checkout_device_name"], "at": r["checkout_at"],
                           "expires": r["checkout_expires"], "mine": _lease_mine(r, user)}
    for k in ("checkout_user_id", "checkout_device", "checkout_device_name", "checkout_at", "checkout_expires"):
        rev.pop(k, None)
    if rev["copied_from_revision_id"]:
        src = conn.execute("SELECT p.number_base, r.rev_no FROM revisions r JOIN procedures p ON p.id = r.procedure_id "
                           "WHERE r.id = ?", (rev["copied_from_revision_id"],)).fetchone()
        rev["copied_from"] = f"{src['number_base']}-V{src['rev_no']:02d}" if src else None
    rev["copies"] = [f"{c['number_base']}-V{c['rev_no']:02d}" for c in conn.execute(
        "SELECT p.number_base, (SELECT MAX(rev_no) FROM revisions WHERE procedure_id = p.id) AS rev_no "
        "FROM procedures p WHERE p.copied_from_revision_id IN (SELECT id FROM revisions WHERE procedure_id = ?)",
        (rev["procedure_id"],))]
    rev["documents"] = [dict(g) for g in conn.execute(
        "SELECT g.id, g.status_at_print, g.pdf_file IS NOT NULL AS has_pdf, g.created_at, u.display_name AS created_by "
        "FROM generated g LEFT JOIN users u ON u.id = g.created_by WHERE g.revision_id = ? ORDER BY g.id DESC",
        (revision_id,))]
    return rev


CHECKOUT_DAYS = 7
_DEVICE_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _lease_active(r):
    return bool(r["checkout_user_id"]) and bool(r["checkout_expires"]) and r["checkout_expires"] > _now()


def _lease_mine(r, user):
    return _lease_active(r) and r["checkout_user_id"] == user["id"] and r["checkout_device"] == user.get("device_id")


def _holder_text(conn, r):
    name = conn.execute("SELECT display_name FROM users WHERE id = ?", (r["checkout_user_id"],)).fetchone()
    return (f"{name['display_name'] if name else 'another user'} on {r['checkout_device_name'] or 'another device'} "
            f"(since {r['checkout_at']} UTC)")


def _check_lock(conn, user, r):
    if _lease_active(r) and not _lease_mine(r, user):
        raise Locked(f"This procedure is checked out to {_holder_text(conn, r)}. "
                     "It must be synced and released there first.")


def _expires():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=CHECKOUT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def checkout(conn, user, revision_id, device_name="", take_over=False):
    """Reserve a draft for offline editing on this device (renewable lease)."""
    require_editor(user)
    device = user.get("device_id") or ""
    if not _DEVICE_RE.match(device):
        raise ServiceError("This device has no valid ID. Reload the app.")
    with tx(conn):
        r = _editable_revision(conn, revision_id)
        if _lease_active(r) and not _lease_mine(r, user):
            own = r["checkout_user_id"] == user["id"]
            if not (take_over and (own or user["role"] == "admin")):
                raise Locked(f"This procedure is checked out to {_holder_text(conn, r)}.")
            audit(conn, user, "revision", revision_id, "checkout_take_over",
                  {"from_user": r["checkout_user_id"], "from_device": r["checkout_device_name"]})
        conn.execute("UPDATE revisions SET checkout_user_id = ?, checkout_device = ?, checkout_device_name = ?, "
                     "checkout_at = COALESCE(CASE WHEN checkout_user_id = ? AND checkout_device = ? "
                     "THEN checkout_at END, ?), checkout_expires = ? WHERE id = ?",
                     (user["id"], device, _clean(device_name, 60) or "device", user["id"], device, _now(),
                      _expires(), revision_id))
        audit(conn, user, "revision", revision_id, "checkout", _clean(device_name, 60))


def release(conn, user, revision_id, force=False):
    with tx(conn):
        r = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
        if not r:
            raise NotFound("Procedure not found.")
        if not _lease_active(r):
            return
        if not _lease_mine(r, user):
            if not (force and (user["role"] == "admin" or r["checkout_user_id"] == user["id"])):
                raise Forbidden("Only the device that checked it out (or an Admin) can release it.")
        conn.execute("UPDATE revisions SET checkout_user_id = NULL, checkout_device = NULL, checkout_device_name = NULL, "
                     "checkout_at = NULL, checkout_expires = NULL WHERE id = ?", (revision_id,))
        audit(conn, user, "revision", revision_id, "release", {"forced": bool(force)})


def _editable_revision(conn, revision_id, expected_version=None, user=None):
    r = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if not r:
        raise NotFound("Procedure not found.")
    if r["status"] not in EDITABLE_STATUSES:
        raise Conflict(f"This revision is {r['status'].replace('_', ' ')} and can't be edited.")
    if user is not None:
        _check_lock(conn, user, r)
    if expected_version is not None and int(expected_version) != r["version"]:
        raise Conflict("Someone else changed this procedure since you opened it. Reload to see their changes.")
    return r


def _touch(conn, user, revision_id):
    """Any edit bumps the version and sends a Ready-for-Review revision back to Draft."""
    conn.execute("UPDATE revisions SET version = version + 1, updated_at = ?, updated_by = ?, "
                 "status = CASE WHEN status IN ('ready_for_review', 'returned') THEN 'draft' ELSE status END WHERE id = ?",
                 (_now(), user["id"], revision_id))


def update_revision(conn, user, revision_id, fields, expected_version):
    require_editor(user)
    with tx(conn):
        _editable_revision(conn, revision_id, expected_version, user=user)
        sets, args = [], []
        for f in LIST_FIELDS:
            if f in fields:
                items = [_clean(i, 500) for i in (fields[f] or []) if _clean(i)]
                sets.append(f"{f} = ?")
                args.append(json.dumps(items[:50]))
        for f in TEXT_FIELDS:
            if f in fields:
                sets.append(f"{f} = ?")
                args.append(_clean(fields[f]))
        if "locks_required_override" in fields:
            v = fields["locks_required_override"]
            if v not in (None, ""):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ServiceError("# of locks must be a whole number.")
                if v < 0:
                    raise ServiceError("# of locks can't be negative.")
            sets.append("locks_required_override = ?")
            args.append(None if v in (None, "") else v)
        if {"effective_date", "review_date", "review_date_manual"} & fields.keys():
            current = conn.execute("SELECT effective_date, review_date, review_date_manual FROM revisions "
                                   "WHERE id = ?", (revision_id,)).fetchone()
            eff = fields.get("effective_date", current["effective_date"]) or None
            manual = bool(fields.get("review_date_manual", current["review_date_manual"]))
            review = fields.get("review_date", current["review_date"]) or None
            try:
                eff_d, review_d = parse_date(eff), parse_date(review)
            except ValueError:
                raise ServiceError("Dates must be YYYY-MM-DD.")
            if not manual:  # default: effective + 1 year, follows the effective date
                review_d = default_review_date(eff_d) if eff_d else None
            sets += ["effective_date = ?", "review_date = ?", "review_date_manual = ?"]
            args += [eff_d.isoformat() if eff_d else None, review_d.isoformat() if review_d else None, int(manual)]
        if sets:
            conn.execute(f"UPDATE revisions SET {', '.join(sets)} WHERE id = ?", (*args, revision_id))
            _touch(conn, user, revision_id)
            audit(conn, user, "revision", revision_id, "update", sorted(k for k in fields if k != "version"))


# ------------------------------------------------------------------ points

def _point(conn, point_id):
    p = conn.execute("SELECT * FROM points WHERE id = ?", (point_id,)).fetchone()
    if not p:
        raise NotFound("Isolation point not found.")
    return p


def _next_label(conn, revision_id, energy_type):
    prefix = {"electrical": "E", "chemical": "C", "mechanical": "M", "pneumatic": "P", "hydraulic": "H",
              "steam": "S", "startstop": "SS", "thermal": "T"}.get(energy_type, "X")
    used = {r["label"] for r in conn.execute("SELECT label FROM points WHERE revision_id = ?", (revision_id,))}
    n = 1
    while f"{prefix}-{n}" in used:
        n += 1
    return f"{prefix}-{n}"


def add_point(conn, user, revision_id, fields, temp_id=None):
    require_editor(user)
    if temp_id:
        existing = _temp_lookup(conn, user, temp_id)
        if existing:
            return existing  # already created by an earlier (retried) sync
    energy = fields.get("energy_type", "")
    if energy not in load_energy_types():
        raise ServiceError("Choose an energy type.")
    with tx(conn):
        _editable_revision(conn, revision_id, user=user)
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM points WHERE revision_id = ?",
                           (revision_id,)).fetchone()[0]
        label = _clean(fields.get("label")) or _next_label(conn, revision_id, energy)
        pid = conn.execute("INSERT INTO points(revision_id, seq, label, energy_type, locks) VALUES (?, ?, ?, ?, ?)",
                           (revision_id, seq, label, energy, 0 if energy == "startstop" else 1)).lastrowid
        _touch(conn, user, revision_id)
        audit(conn, user, "point", pid, "create", label)
        if temp_id:
            _temp_remember(conn, user, temp_id, "point", pid)
    if set(fields) - {"energy_type", "label"}:
        update_point(conn, user, pid, fields, expected_version=None)
    return pid


def update_point(conn, user, point_id, fields, expected_version):
    require_editor(user)
    with tx(conn):
        p = _point(conn, point_id)
        _editable_revision(conn, p["revision_id"], user=user)
        if expected_version is not None and int(expected_version) != p["version"]:
            raise Conflict("Someone else changed this isolation point since you opened it. Reload to see it.")
        sets, args = [], []
        for f in POINT_TEXT_FIELDS:
            if f in fields:
                v = _clean(fields[f], 2000)
                if f == "energy_type" and v not in load_energy_types():
                    raise ServiceError("Choose an energy type.")
                if f == "label":
                    if not v:
                        raise ServiceError("Point label is required (e.g. E-1).")
                    if conn.execute("SELECT 1 FROM points WHERE revision_id = ? AND label = ? AND id != ?",
                                    (p["revision_id"], v, point_id)).fetchone():
                        raise Conflict(f"Label {v} is already used in this procedure.")
                sets.append(f"{f} = ?")
                args.append(v)
        if "locks" in fields:
            try:
                locks = int(fields["locks"])
            except (TypeError, ValueError):
                raise ServiceError("Locks must be a whole number.")
            if not 0 <= locks <= 50:
                raise ServiceError("Locks must be between 0 and 50.")
            sets.append("locks = ?")
            args.append(locks)
        if sets:
            conn.execute(f"UPDATE points SET {', '.join(sets)}, version = version + 1 WHERE id = ?",
                         (*args, point_id))
            _touch(conn, user, p["revision_id"])
            audit(conn, user, "point", point_id, "update", sorted(fields))


def delete_point(conn, user, point_id):
    require_editor(user)
    with tx(conn):
        p = _point(conn, point_id)
        _editable_revision(conn, p["revision_id"], user=user)
        conn.execute("DELETE FROM points WHERE id = ?", (point_id,))
        _resequence(conn, p["revision_id"])
        _touch(conn, user, p["revision_id"])
        audit(conn, user, "point", point_id, "delete", p["label"])


def _resequence(conn, revision_id, ordered_ids=None):
    ids = ordered_ids or [r["id"] for r in conn.execute(
        "SELECT id FROM points WHERE revision_id = ? ORDER BY seq", (revision_id,))]
    for n, pid in enumerate(ids, 1):
        conn.execute("UPDATE points SET seq = ? WHERE id = ?", (n, pid))


def reorder_points(conn, user, revision_id, ordered_ids, expected_version):
    """Set the isolation order. ordered_ids must list every point exactly once."""
    require_editor(user)
    with tx(conn):
        _editable_revision(conn, revision_id, expected_version, user=user)
        existing = {r["id"] for r in conn.execute("SELECT id FROM points WHERE revision_id = ?", (revision_id,))}
        if sorted(map(int, ordered_ids)) != sorted(existing):
            raise ServiceError("Point order doesn't match the procedure's points. Reload and try again.")
        _resequence(conn, revision_id, [int(i) for i in ordered_ids])
        _touch(conn, user, revision_id)
        audit(conn, user, "revision", revision_id, "reorder_points", ordered_ids)


def mark_point_checked(conn, user, point_id):
    """Author confirms a copied point (and any copied photos) match this machine."""
    require_editor(user)
    with tx(conn):
        p = _point(conn, point_id)
        _editable_revision(conn, p["revision_id"], user=user)
        if p["copy_status"] != "needs_check":
            raise ServiceError("This point doesn't need checking.")
        still_copied = conn.execute("SELECT COUNT(*) FROM photos WHERE point_id = ? AND copied_from_photo_id IS NOT NULL",
                                    (point_id,)).fetchone()[0]
        status = "confirmed" if still_copied else "replaced"
        conn.execute("UPDATE points SET copy_status = ?, checked_by = ?, checked_at = ?, version = version + 1 "
                     "WHERE id = ?", (status, user["id"], _now(), point_id))
        _touch(conn, user, p["revision_id"])
        audit(conn, user, "point", point_id, "checked", {"status": status, "copied_photos_kept": still_copied})


# ------------------------------------------------------------------ photos

def photo_path(file_id, kind="display"):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id or ""):
        raise NotFound("Photo not found.")
    folder = data_dir() / "photos" / kind
    matches = list(folder.glob(f"{file_id}.*"))
    if not matches:
        raise NotFound("Photo not found.")
    return matches[0]


def add_photo(conn, user, point_id, content, filename="", temp_id=None, annotations=None):
    """Store the original as uploaded and a display copy (upright, no metadata, 1600 px)."""
    require_editor(user)
    if temp_id:
        existing = _temp_lookup(conn, user, temp_id)
        if existing:
            return existing
    annotations = _clean_annotations(annotations or [])
    if len(content) > MAX_PHOTO_BYTES:
        raise ServiceError("Photo is larger than 25 MB.")
    ext = Path(filename or "").suffix.lower()
    try:
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            fmt = (im.format or "").lower()
            im = ImageOps.exif_transpose(im)
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((DISPLAY_LONG_EDGE, DISPLAY_LONG_EDGE), Image.LANCZOS)
            display = io.BytesIO()
            im.save(display, "JPEG", quality=82, optimize=True)
            size = im.size
    except (UnidentifiedImageError, OSError):
        raise ServiceError("That file isn't a photo this system can read (use JPEG, PNG or HEIC).")
    if ext not in ALLOWED_PHOTO_EXT:
        ext = {"jpeg": ".jpg", "png": ".png", "heif": ".heic", "webp": ".webp"}.get(fmt, ".img")
    file_id = uuid.uuid4().hex
    root = data_dir() / "photos"
    (root / "originals" / f"{file_id}{ext}").write_bytes(content)
    (root / "display" / f"{file_id}.jpg").write_bytes(display.getvalue())
    try:
        with tx(conn):
            p = _point(conn, point_id)
            _editable_revision(conn, p["revision_id"], user=user)
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM photos WHERE point_id = ?",
                               (point_id,)).fetchone()[0]
            phid = conn.execute("INSERT INTO photos(point_id, seq, file_id, original_name, width, height, created_by, "
                                "annotations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (point_id, seq, file_id, _clean(filename, 200), size[0], size[1], user["id"],
                                 json.dumps(annotations))).lastrowid
            if temp_id:
                _temp_remember(conn, user, temp_id, "photo", phid)
            conn.execute("UPDATE points SET version = version + 1 WHERE id = ?", (point_id,))
            _touch(conn, user, p["revision_id"])
            audit(conn, user, "photo", phid, "add", {"point": p["label"], "file": file_id})
    except Exception:
        for kind in ("originals", "display"):
            for f in (root / kind).glob(f"{file_id}.*"):
                f.unlink()
        raise
    return phid


def delete_photo(conn, user, photo_id):
    """Remove a photo from a point. Files stay on disk (they may be shared by copies / kept for the record)."""
    require_editor(user)
    with tx(conn):
        ph = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not ph:
            raise NotFound("Photo not found.")
        p = _point(conn, ph["point_id"])
        _editable_revision(conn, p["revision_id"], user=user)
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        for n, r in enumerate(conn.execute("SELECT id FROM photos WHERE point_id = ? ORDER BY seq",
                                           (ph["point_id"],)).fetchall(), 1):
            conn.execute("UPDATE photos SET seq = ? WHERE id = ?", (n, r["id"]))
        conn.execute("UPDATE points SET version = version + 1 WHERE id = ?", (ph["point_id"],))
        _touch(conn, user, p["revision_id"])
        audit(conn, user, "photo", photo_id, "delete", {"point": p["label"], "file": ph["file_id"]})


def move_photo(conn, user, photo_id, direction):
    require_editor(user)
    with tx(conn):
        ph = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not ph:
            raise NotFound("Photo not found.")
        p = _point(conn, ph["point_id"])
        _editable_revision(conn, p["revision_id"], user=user)
        ids = [r["id"] for r in conn.execute("SELECT id FROM photos WHERE point_id = ? ORDER BY seq",
                                             (ph["point_id"],))]
        i = ids.index(photo_id)
        j = i + (-1 if direction == "up" else 1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for n, pid in enumerate(ids, 1):
                conn.execute("UPDATE photos SET seq = ? WHERE id = ?", (n, pid))
            _touch(conn, user, p["revision_id"])


# ------------------------------------------------------------------ completeness / documents

def to_generate_data(rev):
    """Revision dict (from get_revision) -> data dict for generate()."""
    points = []
    for p in rev["points"]:
        points.append({
            **{k: p[k] for k in POINT_TEXT_FIELDS},
            "locks": p["locks"],
            "copy_status": p["copy_status"],
            "photos": [{"file": str(photo_path(ph["file_id"])), "annotations": ph.get("annotations") or []}
                       for ph in p["photos"]],
        })
    return {
        "site": {"code": rev["site_code"], "name": rev["site_name"]},
        "department": {"code": rev["dept_code"], "name": rev["dept_name"]},
        "asset": {"number": rev["asset_number"], "machine_type": rev["machine_type"],
                  "oem": rev["oem"], "model": rev["model"]},
        "procedure_number_base": rev["number_base"],
        "revision": rev["rev_no"],
        "status": rev["status"],
        "effective_date": rev["effective_date"],
        "review_date": rev["review_date"],
        "document_owner": rev["document_owner"],
        **{f: rev[f] for f in LIST_FIELDS},
        **{f: rev[f] for f in ("shutdown_procedure", "isolate_text", "group_lockout", "stored_energy",
                               "verify_isolation", "servicing_notes", "locks_override_reason")},
        "locks_required_override": rev["locks_required_override"],
        "points": points,
    }


def completeness(conn, user, revision_id):
    rev = get_revision(conn, user, revision_id)
    blocking, warnings = assess(to_generate_data(rev), load_energy_types())
    return {"blocking": blocking, "warnings": warnings, "ready": not blocking}


def set_status(conn, user, revision_id, status):
    """Phase 2 workflow: Draft <-> Ready for Review (approval steps come in Phase 4)."""
    require_editor(user)
    if status not in ("draft", "ready_for_review"):
        raise ServiceError("Unsupported status change.")
    if status == "ready_for_review":
        result = completeness(conn, user, revision_id)
        if not result["ready"]:
            raise ServiceError("Not ready for review:\n- " + "\n- ".join(result["blocking"]))
    with tx(conn):
        r = _editable_revision(conn, revision_id, user=user)
        conn.execute("UPDATE revisions SET status = ?, version = version + 1, updated_at = ?, updated_by = ? "
                     "WHERE id = ?", (status, _now(), user["id"], revision_id))
        audit(conn, user, "revision", revision_id, "status", [r["status"], status])
        if status == "ready_for_review" and _lease_mine(r, user):  # done editing on this device
            conn.execute("UPDATE revisions SET checkout_user_id = NULL, checkout_device = NULL, "
                         "checkout_device_name = NULL, checkout_at = NULL, checkout_expires = NULL WHERE id = ?",
                         (revision_id,))


def generate_documents(conn, user, revision_id, pdf=True):
    """Generate DOCX (+PDF) for the current state and keep it with the revision."""
    rev = get_revision(conn, user, revision_id)
    if user["role"] == "read_only" and rev["status"] != "active":
        raise Forbidden("Read-only accounts can only print Active procedures.")
    pdf = pdf and os.environ.get("LOTOWRITER_PDF", "1") != "0"  # LOTOWRITER_PDF=0: Word files only (tests)
    out_dir = data_dir() / "generated" / rev["procedure_number"] / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    result = generate(to_generate_data(rev), out_dir, pdf=pdf)
    rel = lambda p: str(Path(p).relative_to(data_dir())) if p else None
    with tx(conn):
        gid = conn.execute("INSERT INTO generated(revision_id, status_at_print, docx_file, pdf_file, created_by) "
                           "VALUES (?, ?, ?, ?, ?)",
                           (revision_id, rev["status"], rel(result.docx_path), rel(result.pdf_path), user["id"])).lastrowid
        audit(conn, user, "revision", revision_id, "generate", {"document": gid, "pdf": bool(result.pdf_path)})
    return {"id": gid, "warnings": result.warnings, "has_pdf": bool(result.pdf_path)}


def document_file(conn, user, document_id, kind):
    g = conn.execute("SELECT g.*, r.status FROM generated g JOIN revisions r ON r.id = g.revision_id WHERE g.id = ?",
                     (document_id,)).fetchone()
    if not g:
        raise NotFound("Document not found.")
    if user["role"] == "read_only" and (g["status"] != "active" or g["status_at_print"] != "active"):
        raise Forbidden("Read-only accounts can only open Active procedures.")
    rel = g["pdf_file"] if kind == "pdf" else g["docx_file"]
    if not rel:
        raise NotFound("No PDF was produced for this document.")
    return data_dir() / rel


# ------------------------------------------------------------------ copy from similar machine

_ID_TOKEN = re.compile(r"\b(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b")


def _replace_asset(text, old, new):
    if not old or not text:
        return text, False
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(old) + r"(?![A-Za-z0-9])")
    out, n = pattern.subn(new, text)
    return out, n > 0


def copy_procedure(conn, user, source_revision_id, targets, include=None):
    """Copy a procedure to one or more similar machines.

    targets: [{"asset_id": 5}] or [{"asset_number", "machine_type", "department_id", ...}] (new assets).
    include: {"description": bool, "points": bool, "photos": bool} (all default True).
    Each copy is a new V00 Draft. Every copied point is marked "needs_check" and Ready for Review
    is blocked until someone checks it at the new machine.
    """
    require_editor(user)
    include = {"description": True, "points": True, "photos": True, **(include or {})}
    if not targets:
        raise ServiceError("Choose at least one machine to copy to.")
    src = get_revision(conn, user, source_revision_id)
    created = []
    with tx(conn):
        for t in targets:
            asset_id = t.get("asset_id")
            if not asset_id:
                asset_id = create_asset(conn, user, t, _in_tx=True)
            pid, rid = create_procedure(conn, user, int(asset_id), _in_tx=True, copied_from=source_revision_id)
            target = conn.execute("SELECT asset_number FROM assets WHERE id = ?", (asset_id,)).fetchone()
            old_no, new_no = src["asset_number"], target["asset_number"]

            sets = {
                "effective_date": src["effective_date"],
                "review_date": default_review_date(parse_date(src["effective_date"])).isoformat()
                if src["effective_date"] else None,
                "review_date_manual": 0,
                "document_owner": src["document_owner"],
                "change_reason": f"Copied from {src['procedure_number']}",
            }
            replaced_desc = []
            if include["description"]:
                for f in LIST_FIELDS:
                    items = []
                    for i in src[f]:
                        new_i, hit = _replace_asset(i, old_no, new_no)
                        items.append(new_i)
                        if hit:
                            replaced_desc.append(f)
                    sets[f] = json.dumps(items)
                for f in ("shutdown_procedure", "isolate_text", "group_lockout", "stored_energy",
                          "verify_isolation", "servicing_notes", "locks_override_reason"):
                    sets[f], hit = _replace_asset(src[f], old_no, new_no)
                    if hit:
                        replaced_desc.append(f)
                sets["locks_required_override"] = src["locks_required_override"]
            conn.execute(f"UPDATE revisions SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), rid))

            if include["points"]:
                for p in src["points"]:
                    values, replaced = {}, []
                    for f in POINT_TEXT_FIELDS:
                        values[f], hit = _replace_asset(p[f], old_no, new_no)
                        if hit:
                            replaced.append(f)
                    check_tokens = sorted({tok for f in POINT_CHECK_FIELDS for tok in _ID_TOKEN.findall(values[f])
                                           if tok.lower() != new_no.lower()})
                    flags = {"replaced_fields": replaced, "check_tokens": check_tokens,
                             "source_point": p["label"]}
                    new_pid = conn.execute(
                        f"INSERT INTO points(revision_id, seq, locks, copy_status, copy_flags, "
                        f"{', '.join(POINT_TEXT_FIELDS)}) VALUES (?, ?, ?, 'needs_check', ?, "
                        f"{', '.join('?' for _ in POINT_TEXT_FIELDS)})",
                        (rid, p["seq"], p["locks"], json.dumps(flags), *values.values())).lastrowid
                    if include["photos"]:
                        for ph in p["photos"]:  # same file until retaken (no duplicate storage)
                            conn.execute("INSERT INTO photos(point_id, seq, file_id, original_name, width, height, "
                                         "copied_from_photo_id, created_by, annotations) "
                                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                         (new_pid, ph["seq"], ph["file_id"], ph["original_name"], ph["width"],
                                          ph["height"], ph["id"], user["id"], json.dumps(ph["annotations"])))
            audit(conn, user, "procedure", pid, "copied_from",
                  {"source": src["procedure_number"], "include": include, "replaced_description_fields": replaced_desc})
            created.append({"procedure_id": pid, "revision_id": rid,
                            "procedure_number": conn.execute("SELECT number_base FROM procedures WHERE id = ?",
                                                             (pid,)).fetchone()[0] + "-V00"})
    return created


# ------------------------------------------------------------------ photo markup

ANNOTATION_TYPES = {"arrow", "ellipse", "rect", "text"}
ANNOTATION_COLORS = {"red", "yellow", "white", "black"}
MAX_ANNOTATIONS = 40


def _clean_annotations(items):
    """Validate markup shapes. Coordinates are fractions of the image width/height."""
    if not isinstance(items, list) or len(items) > MAX_ANNOTATIONS:
        raise ServiceError(f"Photo markup: at most {MAX_ANNOTATIONS} shapes.")
    out = []
    for a in items:
        if not isinstance(a, dict) or a.get("type") not in ANNOTATION_TYPES:
            raise ServiceError("Photo markup: unknown shape.")
        color = a.get("color", "red")
        if color not in ANNOTATION_COLORS:
            raise ServiceError("Photo markup: unknown color.")
        shape = {"type": a["type"], "color": color}
        keys = {"arrow": ("x1", "y1", "x2", "y2"), "ellipse": ("x", "y", "w", "h"),
                "rect": ("x", "y", "w", "h"), "text": ("x", "y")}[a["type"]]
        for k in keys:
            try:
                v = float(a[k])
            except (KeyError, TypeError, ValueError):
                raise ServiceError("Photo markup: bad coordinates.")
            if not -0.5 <= v <= 1.5:
                raise ServiceError("Photo markup: shape is outside the photo.")
            shape[k] = round(v, 4)
        if a["type"] == "text":
            shape["text"] = _clean(a.get("text"), 60)
            if not shape["text"]:
                continue
        out.append(shape)
    return out


def set_photo_annotations(conn, user, photo_id, annotations):
    require_editor(user)
    clean = _clean_annotations(annotations)
    with tx(conn):
        ph = conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if not ph:
            raise NotFound("Photo not found.")
        p = _point(conn, ph["point_id"])
        _editable_revision(conn, p["revision_id"], user=user)
        conn.execute("UPDATE photos SET annotations = ? WHERE id = ?", (json.dumps(clean), photo_id))
        conn.execute("UPDATE points SET version = version + 1 WHERE id = ?", (ph["point_id"],))
        _touch(conn, user, p["revision_id"])
        audit(conn, user, "photo", photo_id, "annotate", {"shapes": len(clean)})


# ------------------------------------------------------------------ offline sync

def _temp_lookup(conn, user, temp_id):
    r = conn.execute("SELECT real_id FROM temp_ids WHERE device_id = ? AND temp_id = ?",
                     (user.get("device_id") or "", str(temp_id))).fetchone()
    return r["real_id"] if r else None


def _temp_remember(conn, user, temp_id, kind, real_id):
    conn.execute("INSERT OR IGNORE INTO temp_ids(device_id, temp_id, kind, real_id) VALUES (?, ?, ?, ?)",
                 (user.get("device_id") or "", str(temp_id), kind, real_id))


def resolve_id(conn, user, value):
    """Real id for an id that may be an offline temp id ("t-...")."""
    if isinstance(value, str) and value.startswith("t-"):
        real = _temp_lookup(conn, user, value)
        if real is None:
            raise NotFound("Refers to an item that was never saved on the server.")
        return real
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ServiceError("Bad item reference.")


def _ensure_my_lease(conn, user, revision_id):
    """Sync needs this device's checkout; quietly renew it, or re-take it if it merely expired."""
    r = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if not r:
        raise NotFound("Procedure not found.")
    if _lease_mine(r, user):
        conn.execute("UPDATE revisions SET checkout_expires = ? WHERE id = ?", (_expires(), revision_id))
        return False
    if _lease_active(r):
        raise Locked(f"This procedure is now checked out to {_holder_text(conn, r)}. "
                     "Changes saved on this device could not be uploaded.")
    return True  # caller re-acquires outside this transaction


def _lease_for_sync(conn, user, revision_id):
    with tx(conn):
        retake = _ensure_my_lease(conn, user, revision_id)
    if retake:
        checkout(conn, user, revision_id, device_name=user.get("device_name") or "device")


def _apply_op(conn, user, revision_id, op):
    kind = op.get("type")
    if kind == "revision.update":
        update_revision(conn, user, revision_id, op.get("fields") or {}, expected_version=None)
        return {}
    if kind == "point.add":
        return {"id": add_point(conn, user, revision_id, op.get("fields") or {}, temp_id=op.get("temp_id"))}
    if kind == "point.update":
        update_point(conn, user, resolve_id(conn, user, op.get("point")), op.get("fields") or {},
                     expected_version=None)
        return {}
    if kind == "point.delete":
        try:
            delete_point(conn, user, resolve_id(conn, user, op.get("point")))
        except NotFound:
            pass  # already gone
        return {}
    if kind == "point.reorder":
        wanted = []
        for i in op.get("ids") or []:
            try:
                wanted.append(resolve_id(conn, user, i))
            except NotFound:
                pass
        existing = [r["id"] for r in conn.execute("SELECT id FROM points WHERE revision_id = ? ORDER BY seq",
                                                  (revision_id,))]
        ids = [i for i in wanted if i in existing] + [i for i in existing if i not in wanted]
        reorder_points(conn, user, revision_id, ids, expected_version=None)
        return {}
    if kind == "point.check":
        try:
            mark_point_checked(conn, user, resolve_id(conn, user, op.get("point")))
        except ServiceError as e:
            if "doesn't need checking" not in str(e):
                raise
        return {}
    if kind == "photo.delete":
        try:
            delete_photo(conn, user, resolve_id(conn, user, op.get("photo")))
        except NotFound:
            pass
        return {}
    if kind == "photo.move":
        move_photo(conn, user, resolve_id(conn, user, op.get("photo")), op.get("direction"))
        return {}
    if kind == "photo.annotate":
        set_photo_annotations(conn, user, resolve_id(conn, user, op.get("photo")), op.get("annotations") or [])
        return {}
    raise ServiceError(f"Unknown change type {kind!r}.")


def sync(conn, user, revision_id, ops):
    """Apply changes made offline, in order. Each change succeeds or fails on its own.

    Safe to retry: creations carry temp ids, and the other changes are idempotent.
    """
    require_editor(user)
    _lease_for_sync(conn, user, revision_id)
    results = []
    for op in (ops or [])[:500]:
        try:
            results.append({"op_id": op.get("op_id"), "ok": True, **_apply_op(conn, user, revision_id, op)})
        except Locked:
            raise
        except ServiceError as e:
            results.append({"op_id": op.get("op_id"), "ok": False, "error": str(e)})
    return results


def sync_photo(conn, user, revision_id, point_ref, content, filename, temp_id, annotations):
    require_editor(user)
    _lease_for_sync(conn, user, revision_id)
    point_id = resolve_id(conn, user, point_ref)
    if _point(conn, point_id)["revision_id"] != revision_id:
        raise ServiceError("Photo belongs to a different procedure.")
    return add_photo(conn, user, point_id, content, filename, temp_id=temp_id, annotations=annotations)


# ------------------------------------------------------------------ users (admin)

def list_users(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, username, display_name, role, active, must_change_password, last_login, created_at, "
        "locked_until FROM users ORDER BY username")]


def create_user(conn, admin, username, display_name, role, temp_password):
    from .auth import ROLES, hash_password, password_problem
    username = _clean(username, 50)
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,50}", username):
        raise ServiceError("User name: 2-50 letters, digits, dot, dash or underscore.")
    if role not in ROLES:
        raise ServiceError("Choose a role.")
    problem = password_problem(temp_password, username)
    if problem:
        raise ServiceError(problem)
    with tx(conn):
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise Conflict(f"User name {username} is taken.")
        uid = conn.execute("INSERT INTO users(username, display_name, role, password_hash, must_change_password) "
                           "VALUES (?, ?, ?, ?, 1)",
                           (username, _clean(display_name, 100) or username, role, hash_password(temp_password))
                           ).lastrowid
        audit(conn, admin, "user", uid, "create", {"username": username, "role": role})
    return uid


def update_user(conn, admin, user_id, fields):
    from .auth import ROLES, active_admin_count, end_sessions
    with tx(conn):
        u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not u:
            raise NotFound("User not found.")
        role = fields.get("role", u["role"])
        active = int(bool(fields.get("active", u["active"])))
        if role not in ROLES:
            raise ServiceError("Choose a role.")
        losing_admin = u["role"] == "admin" and u["active"] and (role != "admin" or not active)
        if losing_admin and active_admin_count(conn) <= 1:
            raise Conflict("There must always be at least one active Admin account.")
        conn.execute("UPDATE users SET display_name = ?, role = ?, active = ? WHERE id = ?",
                     (_clean(fields.get("display_name", u["display_name"]), 100) or u["username"], role, active,
                      user_id))
        if not active:
            end_sessions(conn, user_id)
        audit(conn, admin, "user", user_id, "update", {"role": role, "active": active})


def reset_password(conn, admin, user_id, temp_password):
    from .auth import end_sessions, password_problem, set_password
    u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u:
        raise NotFound("User not found.")
    problem = password_problem(temp_password, u["username"])
    if problem:
        raise ServiceError(problem)
    with tx(conn):
        set_password(conn, user_id, temp_password, must_change=True)
        end_sessions(conn, user_id)
        audit(conn, admin, "user", user_id, "reset_password")
