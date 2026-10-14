"""Review & approval workflow, revisions, scans, periodic reviews and the dashboard.

    Draft ──► Ready for review ──► Printed for review ──► Approved ──► Active ──► Superseded
      ▲                                   │
      └──── Returned with markups ◄───────┘

Reviewers work on paper; the app records what they decided. Nothing is valid for use
until it is Active, and everything before Active prints with a DRAFT watermark.
"""
import datetime as dt
import io
import json
import uuid

from PIL import Image, UnidentifiedImageError

from .db import data_dir
from .generate import default_review_date, parse_date
from .service import (LIST_FIELDS, POINT_TEXT_FIELDS, Conflict, Forbidden, NotFound, ServiceError, _clean, _now,
                      audit, completeness, generate_documents, get_revision, require_editor, tx)

OPEN_STATUSES = ("draft", "ready_for_review", "printed_for_review", "returned", "approved")
SCAN_KINDS = {"markup": "Marked-up review copy", "approval": "Signed approval copy", "other": "Other"}
MAX_SCAN_BYTES = 25 * 1024 * 1024
DUE_SOON_DAYS = 60
REVISION_TEXT_FIELDS = ("shutdown_procedure", "isolate_text", "group_lockout", "stored_energy", "verify_isolation",
                        "servicing_notes", "locks_override_reason", "document_owner")


def _today():
    return dt.date.today()


def _rev_row(conn, revision_id):
    r = conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if not r:
        raise NotFound("Procedure not found.")
    return r


def _require_status(r, *allowed):
    if r["status"] not in allowed:
        names = " or ".join(s.replace("_", " ") for s in allowed)
        raise Conflict(f"This revision is {r['status'].replace('_', ' ')}; it must be {names} for that.")


def _set(conn, revision_id, user, **cols):
    cols["updated_at"] = _now()
    cols["updated_by"] = user["id"]
    conn.execute(f"UPDATE revisions SET {', '.join(k + ' = ?' for k in cols)}, version = version + 1 WHERE id = ?",
                 (*cols.values(), revision_id))


def _release_checkout(conn, revision_id):
    conn.execute("UPDATE revisions SET checkout_user_id = NULL, checkout_device = NULL, checkout_device_name = NULL, "
                 "checkout_at = NULL, checkout_expires = NULL WHERE id = ?", (revision_id,))


def _date(value, name, allow_future=False):
    try:
        d = parse_date(value)
    except ValueError:
        raise ServiceError(f"{name} must be a date (YYYY-MM-DD).")
    if not d:
        raise ServiceError(f"{name} is required.")
    if not allow_future and d > _today():
        raise ServiceError(f"{name} can't be in the future.")
    return d


# ------------------------------------------------------------------ status changes

def print_for_review(conn, user, revision_id):
    """Ready for review -> Printed for review, and print the review copy (DRAFT watermark).

    From here the content is frozen: reviewers have this paper copy. Changes go through
    'Returned with markups'.
    """
    require_editor(user)
    result = completeness(conn, user, revision_id)
    if not result["ready"]:
        raise ServiceError("Not ready for review:\n- " + "\n- ".join(result["blocking"]))
    with tx(conn):
        r = _rev_row(conn, revision_id)
        _require_status(r, "ready_for_review")
        _set(conn, revision_id, user, status="printed_for_review", printed_for_review_at=_now())
        _release_checkout(conn, revision_id)
        audit(conn, user, "revision", revision_id, "status", ["ready_for_review", "printed_for_review"])
    try:
        return generate_documents(conn, user, revision_id, pdf=True)
    except Exception:
        with tx(conn):  # no review copy -> don't pretend it was printed
            _set(conn, revision_id, user, status="ready_for_review", printed_for_review_at=None)
        raise


def return_with_markups(conn, user, revision_id, note):
    require_editor(user)
    note = _clean(note, 2000)
    if not note:
        raise ServiceError("Describe what the reviewers marked up (or upload a scan of the marked-up copy and say so).")
    with tx(conn):
        r = _rev_row(conn, revision_id)
        _require_status(r, "printed_for_review")
        _set(conn, revision_id, user, status="returned", returned_note=note)
        audit(conn, user, "revision", revision_id, "status", {"to": "returned", "note": note})


def record_approval(conn, user, revision_id, approved_by, approved_date, note=""):
    """Record the reviewers' paper approval. The app records it; it does not approve anything itself."""
    require_editor(user)
    approved_by = _clean(approved_by, 300)
    if not approved_by:
        raise ServiceError("Enter the name(s) of the reviewer(s) who approved the printed copy.")
    d = _date(approved_date, "Approval date")
    with tx(conn):
        r = _rev_row(conn, revision_id)
        _require_status(r, "printed_for_review")
        if r["printed_for_review_at"] and d.isoformat() < r["printed_for_review_at"][:10]:
            raise ServiceError("Approval date is before the review copy was printed.")
        _set(conn, revision_id, user, status="approved", approved_by=approved_by, approved_date=d.isoformat(),
             approval_note=_clean(note, 2000), approval_recorded_by=user["id"])
        audit(conn, user, "revision", revision_id, "status",
              {"to": "approved", "approved_by": approved_by, "approved_date": d.isoformat()})


def withdraw_approval(conn, user, revision_id):
    """Undo a mistakenly recorded approval (back to Printed for review)."""
    require_editor(user)
    with tx(conn):
        r = _rev_row(conn, revision_id)
        _require_status(r, "approved")
        _set(conn, revision_id, user, status="printed_for_review", approved_by="", approved_date=None,
             approval_note="", approval_recorded_by=None)
        audit(conn, user, "revision", revision_id, "status", ["approved", "printed_for_review"])


def activate(conn, user, revision_id, effective_date=None):
    """Approved -> Active. The previous Active revision becomes Superseded. Prints the final copy."""
    require_editor(user)
    with tx(conn):
        r = _rev_row(conn, revision_id)
        _require_status(r, "approved")
        eff = _date(effective_date or r["effective_date"] or _today().isoformat(), "Effective date", allow_future=True)
        review = parse_date(r["review_date"]) if r["review_date_manual"] and r["review_date"] else default_review_date(eff)
        if review <= eff:
            review = default_review_date(eff)
        previous = conn.execute("SELECT id, rev_no FROM revisions WHERE procedure_id = ? AND status = 'active'",
                                (r["procedure_id"],)).fetchall()
        for p in previous:
            _set(conn, p["id"], user, status="superseded", superseded_at=_now())
            audit(conn, user, "revision", p["id"], "status", {"to": "superseded", "by_revision": r["rev_no"]})
        _set(conn, revision_id, user, status="active", activated_at=_now(), activated_by=user["id"],
             effective_date=eff.isoformat(), review_date=review.isoformat())
        conn.execute("UPDATE procedures SET flag_reason = '', flagged_by = NULL, flagged_at = NULL WHERE id = ?",
                     (r["procedure_id"],))
        audit(conn, user, "revision", revision_id, "status", {"to": "active", "effective_date": eff.isoformat()})
    try:
        doc = generate_documents(conn, user, revision_id, pdf=True)
    except Exception as e:  # activation stands; the final copy can be printed again from Check & print
        doc = {"warnings": [f"Final document not created: {e}"]}
    return doc


def new_revision(conn, user, revision_id, reason):
    """Start V(n+1) from the Active revision. The Active one stays in use until the new one is activated."""
    require_editor(user)
    reason = _clean(reason, 1000)
    if not reason:
        raise ServiceError("Say why the procedure is being revised (e.g. machine modified, annual review changes).")
    src = get_revision(conn, user, revision_id)
    if src["status"] != "active":
        raise Conflict("New revisions are started from the Active revision.")
    with tx(conn):
        open_rev = conn.execute(f"SELECT rev_no FROM revisions WHERE procedure_id = ? AND status IN "
                                f"({','.join('?' * len(OPEN_STATUSES))})",
                                (src["procedure_id"], *OPEN_STATUSES)).fetchone()
        if open_rev:
            raise Conflict(f"V{open_rev['rev_no']:02d} is already in progress for this procedure.")
        rev_no = conn.execute("SELECT MAX(rev_no) FROM revisions WHERE procedure_id = ?",
                              (src["procedure_id"],)).fetchone()[0] + 1
        cols = {f: json.dumps(src[f]) for f in LIST_FIELDS}
        cols.update({f: src[f] for f in REVISION_TEXT_FIELDS})
        cols.update({"locks_required_override": src["locks_required_override"], "change_reason": reason,
                     "effective_date": None, "review_date": None, "review_date_manual": 0})
        rid = conn.execute(
            f"INSERT INTO revisions(procedure_id, rev_no, created_by, updated_by, {', '.join(cols)}) "
            f"VALUES (?, ?, ?, ?, {', '.join('?' * len(cols))})",
            (src["procedure_id"], rev_no, user["id"], user["id"], *cols.values())).lastrowid
        for p in src["points"]:
            pid = conn.execute(
                f"INSERT INTO points(revision_id, seq, locks, {', '.join(POINT_TEXT_FIELDS)}) "
                f"VALUES (?, ?, ?, {', '.join('?' * len(POINT_TEXT_FIELDS))})",
                (rid, p["seq"], p["locks"], *[p[f] for f in POINT_TEXT_FIELDS])).lastrowid
            for ph in p["photos"]:  # same machine: photos carry over as-is (shared files)
                conn.execute("INSERT INTO photos(point_id, seq, file_id, original_name, width, height, created_by, "
                             "annotations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (pid, ph["seq"], ph["file_id"], ph["original_name"], ph["width"], ph["height"],
                              user["id"], json.dumps(ph["annotations"])))
        audit(conn, user, "revision", rid, "create", {"from": src["procedure_number"], "reason": reason})
    return rid


# ------------------------------------------------------------------ flags, periodic reviews, copies

def flag_for_revision(conn, user, revision_id, reason):
    require_editor(user)
    reason = _clean(reason, 1000)
    if not reason:
        raise ServiceError("Say why this procedure needs revising (e.g. guard added, new air drop).")
    with tx(conn):
        r = _rev_row(conn, revision_id)
        conn.execute("UPDATE procedures SET flag_reason = ?, flagged_by = ?, flagged_at = ? WHERE id = ?",
                     (reason, user["id"], _now(), r["procedure_id"]))
        audit(conn, user, "procedure", r["procedure_id"], "flag", reason)


def clear_flag(conn, user, revision_id):
    require_editor(user)
    with tx(conn):
        r = _rev_row(conn, revision_id)
        conn.execute("UPDATE procedures SET flag_reason = '', flagged_by = NULL, flagged_at = NULL WHERE id = ?",
                     (r["procedure_id"],))
        audit(conn, user, "procedure", r["procedure_id"], "unflag")


def record_inspection(conn, user, revision_id, inspected_date, inspector, note=""):
    """Periodic (annual) review of the Active procedure that found no changes needed.

    It moves the next-review due date to one year after the inspection. The printed
    document isn't re-issued; if anything needs to change, start a new revision instead.
    """
    require_editor(user)
    inspector = _clean(inspector, 200)
    if not inspector:
        raise ServiceError("Enter who performed the periodic review.")
    d = _date(inspected_date, "Review date")
    with tx(conn):
        r = _rev_row(conn, revision_id)
        _require_status(r, "active")
        iid = conn.execute("INSERT INTO inspections(procedure_id, revision_id, inspected_date, inspector, note, "
                           "recorded_by) VALUES (?, ?, ?, ?, ?, ?)",
                           (r["procedure_id"], revision_id, d.isoformat(), inspector, _clean(note, 2000),
                            user["id"])).lastrowid
        audit(conn, user, "procedure", r["procedure_id"], "periodic_review",
              {"id": iid, "date": d.isoformat(), "inspector": inspector})


def acknowledge_source_change(conn, user, revision_id):
    """'Reviewed – no change needed' for a copy whose source procedure was revised."""
    require_editor(user)
    with tx(conn):
        r = _rev_row(conn, revision_id)
        src = conn.execute("SELECT MAX(r2.rev_no) FROM procedures p JOIN revisions r1 ON r1.id = p.copied_from_revision_id "
                           "JOIN revisions r2 ON r2.procedure_id = r1.procedure_id AND r2.status = 'active' "
                           "WHERE p.id = ?", (r["procedure_id"],)).fetchone()[0]
        conn.execute("UPDATE procedures SET source_ack_rev = ? WHERE id = ?", (src, r["procedure_id"]))
        audit(conn, user, "procedure", r["procedure_id"], "source_change_reviewed", {"source_rev": src})


def _next_due(review_date, last_inspection):
    due = parse_date(review_date) if review_date else None
    if last_inspection:
        by_inspection = default_review_date(parse_date(last_inspection))
        due = max(due, by_inspection) if due else by_inspection
    return due


# ------------------------------------------------------------------ scans

def add_scan(conn, user, revision_id, kind, content, filename="", note=""):
    require_editor(user)
    if kind not in SCAN_KINDS:
        raise ServiceError("Choose what kind of scan this is.")
    if len(content) > MAX_SCAN_BYTES:
        raise ServiceError("Scan is larger than 25 MB.")
    if content[:5] == b"%PDF-":
        ctype, ext = "application/pdf", ".pdf"
    else:
        try:
            with Image.open(io.BytesIO(content)) as im:
                im.verify()
                fmt = (im.format or "").upper()
        except (UnidentifiedImageError, OSError):
            raise ServiceError("Upload the scan as a PDF or a photo (JPEG/PNG/HEIC).")
        ctype, ext = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"),
                      "HEIF": ("image/heic", ".heic"), "WEBP": ("image/webp", ".webp")}.get(fmt, (None, None))
        if not ctype:
            raise ServiceError("Upload the scan as a PDF or a photo (JPEG/PNG/HEIC).")
    _rev_row(conn, revision_id)
    file_id = uuid.uuid4().hex
    path = data_dir() / "scans" / f"{file_id}{ext}"
    path.write_bytes(content)
    try:
        with tx(conn):
            sid = conn.execute("INSERT INTO scans(revision_id, kind, file_id, original_name, content_type, note, "
                               "uploaded_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (revision_id, kind, file_id, _clean(filename, 200), ctype, _clean(note, 500),
                                user["id"])).lastrowid
            audit(conn, user, "revision", revision_id, "scan", {"id": sid, "kind": kind})
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return sid


def scan_file(conn, user, scan_id):
    if user["role"] == "read_only":
        raise Forbidden("Scans are available to Full access accounts.")
    s = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if not s:
        raise NotFound("Scan not found.")
    matches = list((data_dir() / "scans").glob(f"{s['file_id']}.*"))
    if not matches:
        raise NotFound("Scan file is missing.")
    return matches[0], s["content_type"], s["original_name"] or matches[0].name


# ------------------------------------------------------------------ revision details for the UI

def full_revision(conn, user, revision_id):
    """get_revision() plus workflow details: history, scans, periodic reviews, flags, events."""
    rev = get_revision(conn, user, revision_id)
    r = _rev_row(conn, revision_id)
    proc = conn.execute("SELECT * FROM procedures WHERE id = ?", (rev["procedure_id"],)).fetchone()
    for k in ("printed_for_review_at", "returned_note", "approved_by", "approved_date", "approval_note",
              "activated_at", "superseded_at"):
        rev[k] = r[k]
    name = lambda uid: (conn.execute("SELECT display_name FROM users WHERE id = ?", (uid,)).fetchone() or [None])[0]
    rev["approval_recorded_by"] = name(r["approval_recorded_by"]) if r["approval_recorded_by"] else None
    rev["flag"] = ({"reason": proc["flag_reason"], "by": name(proc["flagged_by"]), "at": proc["flagged_at"]}
                   if proc["flag_reason"] else None)
    revisions = conn.execute("SELECT id, rev_no, status, effective_date, approved_by, approved_date, change_reason, "
                             "activated_at, superseded_at, created_at FROM revisions WHERE procedure_id = ? "
                             "ORDER BY rev_no DESC", (rev["procedure_id"],)).fetchall()
    if user["role"] == "read_only":
        revisions = [x for x in revisions if x["status"] == "active"]
    rev["history"] = [dict(x) | {"procedure_number": f"{rev['number_base']}-V{x['rev_no']:02d}"} for x in revisions]
    active = next((x for x in revisions if x["status"] == "active"), None)
    rev["active_revision_id"] = active["id"] if active else None
    open_rev = next((x for x in revisions if x["status"] in OPEN_STATUSES), None)
    rev["open_revision_id"] = open_rev["id"] if open_rev else None
    if user["role"] == "read_only":
        rev["scans"], rev["events"] = [], []
    else:
        rev["scans"] = [dict(s) | {"kind_label": SCAN_KINDS[s["kind"]], "uploaded_by": name(s["uploaded_by"])}
                        for s in conn.execute("SELECT id, kind, original_name, content_type, note, uploaded_by, "
                                              "created_at FROM scans WHERE revision_id = ? ORDER BY id DESC",
                                              (revision_id,))]
        rev_ids = [x["id"] for x in revisions]
        rev["events"] = [dict(e) for e in conn.execute(
            f"SELECT a.at, a.action, a.detail, a.entity, a.entity_id, u.display_name AS who FROM audit_log a "
            f"LEFT JOIN users u ON u.id = a.user_id WHERE (a.entity = 'revision' AND a.entity_id IN "
            f"({','.join('?' * len(rev_ids))})) OR (a.entity = 'procedure' AND a.entity_id = ?) "
            f"ORDER BY a.id DESC LIMIT 200", (*rev_ids, rev["procedure_id"]))]
    inspections = [dict(i) for i in conn.execute(
        "SELECT inspected_date, inspector, note, created_at FROM inspections WHERE procedure_id = ? "
        "ORDER BY inspected_date DESC", (rev["procedure_id"],))]
    rev["inspections"] = inspections
    if rev["status"] == "active":
        due = _next_due(rev["review_date"], inspections[0]["inspected_date"] if inspections else None)
        rev["next_review_due"] = due.isoformat() if due else None
    # Copy whose source procedure has been revised since?
    rev["source_newer"] = None
    if rev["copied_from_revision_id"]:
        src = conn.execute(
            "SELECT p.number_base, r1.rev_no AS copied_rev, MAX(r2.rev_no) AS active_rev FROM revisions r1 "
            "JOIN procedures p ON p.id = r1.procedure_id "
            "JOIN revisions r2 ON r2.procedure_id = r1.procedure_id AND r2.status = 'active' WHERE r1.id = ?",
            (rev["copied_from_revision_id"],)).fetchone()
        if src and src["active_rev"] is not None and src["active_rev"] > src["copied_rev"] \
                and (proc["source_ack_rev"] or -1) < src["active_rev"]:
            rev["source_newer"] = f"{src['number_base']}-V{src['active_rev']:02d}"
    return rev


# ------------------------------------------------------------------ dashboard

def dashboard(conn, user):
    today = _today()
    soon = today + dt.timedelta(days=DUE_SOON_DAYS)
    base = ("SELECT p.id AS procedure_id, p.number_base, r.id AS revision_id, r.rev_no, r.status, r.review_date, "
            "r.updated_at, a.asset_number, a.machine_type, d.name AS dept_name, p.flag_reason, p.flagged_at, "
            "(SELECT MAX(inspected_date) FROM inspections i WHERE i.procedure_id = p.id) AS last_inspection "
            "FROM procedures p JOIN revisions r ON r.procedure_id = p.id JOIN assets a ON a.id = p.asset_id "
            "JOIN departments d ON d.id = a.department_id ")

    def row(x):
        out = dict(x)
        out["procedure_number"] = f"{x['number_base']}-V{x['rev_no']:02d}"
        return out

    active = [row(x) for x in conn.execute(base + "WHERE r.status = 'active' ORDER BY a.asset_number")]
    overdue, due_soon = [], []
    for a in active:
        due = _next_due(a["review_date"], a["last_inspection"])
        a["next_review_due"] = due.isoformat() if due else None
        if due and due < today:
            overdue.append(a)
        elif due and due <= soon:
            due_soon.append(a)
    result = {
        "today": today.isoformat(),
        "counts": {"active": len(active)},
        "overdue": sorted(overdue, key=lambda a: a["next_review_due"]),
        "due_soon": sorted(due_soon, key=lambda a: a["next_review_due"]),
        "flagged": [a for a in active if a["flag_reason"]],
    }
    if user["role"] == "read_only":
        return result
    open_rows = [row(x) for x in conn.execute(
        base + f"WHERE r.status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY r.updated_at",
        OPEN_STATUSES)]
    result["in_progress"] = {s: [x for x in open_rows if x["status"] == s] for s in OPEN_STATUSES}
    for s in OPEN_STATUSES:
        result["counts"][s] = len(result["in_progress"][s])
    result["no_procedure"] = [dict(a) for a in conn.execute(
        "SELECT a.id, a.asset_number, a.machine_type, d.name AS dept_name FROM assets a "
        "JOIN departments d ON d.id = a.department_id LEFT JOIN procedures p ON p.asset_id = a.id "
        "WHERE p.id IS NULL AND a.status = 'active' ORDER BY a.asset_number")]
    copies = []
    for c in conn.execute(
            "SELECT p.id, p.number_base, p.source_ack_rev, r1.rev_no AS copied_rev, sp.number_base AS src_base, "
            "(SELECT MAX(rev_no) FROM revisions WHERE procedure_id = sp.id AND status = 'active') AS src_active, "
            "(SELECT id FROM revisions WHERE procedure_id = p.id ORDER BY rev_no DESC LIMIT 1) AS revision_id "
            "FROM procedures p JOIN revisions r1 ON r1.id = p.copied_from_revision_id "
            "JOIN procedures sp ON sp.id = r1.procedure_id"):
        if c["src_active"] is not None and c["src_active"] > c["copied_rev"] and (c["source_ack_rev"] or -1) < c["src_active"]:
            copies.append({"revision_id": c["revision_id"], "procedure": c["number_base"],
                           "source": f"{c['src_base']}-V{c['src_active']:02d}"})
    result["source_revised"] = copies
    result["checked_out"] = [dict(x) | {"procedure_number": f"{x['number_base']}-V{x['rev_no']:02d}"} for x in conn.execute(
        "SELECT r.id AS revision_id, p.number_base, r.rev_no, u.display_name AS user_name, r.checkout_device_name, "
        "r.checkout_at FROM revisions r JOIN procedures p ON p.id = r.procedure_id JOIN users u ON u.id = r.checkout_user_id "
        "WHERE r.checkout_expires > ? ORDER BY r.checkout_at", (_now(),))]
    return result


# ------------------------------------------------------------------ read-only photo access

def photo_visible_to_read_only(conn, file_id):
    return conn.execute("SELECT 1 FROM photos ph JOIN points pt ON pt.id = ph.point_id "
                        "JOIN revisions r ON r.id = pt.revision_id WHERE ph.file_id = ? AND r.status = 'active' "
                        "LIMIT 1", (file_id,)).fetchone() is not None


def transition(conn, user, revision_id, action, body):
    """Single entry point for the workflow buttons."""
    actions = {
        "print_for_review": lambda: print_for_review(conn, user, revision_id),
        "return": lambda: return_with_markups(conn, user, revision_id, body.get("note")),
        "approve": lambda: record_approval(conn, user, revision_id, body.get("approved_by"), body.get("approved_date"),
                                           body.get("note", "")),
        "withdraw_approval": lambda: withdraw_approval(conn, user, revision_id),
        "activate": lambda: activate(conn, user, revision_id, body.get("effective_date")),
        "new_revision": lambda: {"revision_id": new_revision(conn, user, revision_id, body.get("reason"))},
        "flag": lambda: flag_for_revision(conn, user, revision_id, body.get("reason")),
        "clear_flag": lambda: clear_flag(conn, user, revision_id),
        "periodic_review": lambda: record_inspection(conn, user, revision_id, body.get("inspected_date"),
                                                     body.get("inspector"), body.get("note", "")),
        "ack_source_change": lambda: acknowledge_source_change(conn, user, revision_id),
    }
    if action not in actions:
        raise ServiceError("Unknown action.")
    return actions[action]() or {}
