"""SQLite storage. One file at <data dir>/db/lotowriter.sqlite3."""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir():
    """Where the database, photos and generated files live (LOTOWRITER_DATA, default ./data)."""
    d = Path(os.environ.get("LOTOWRITER_DATA", ROOT / "data"))
    for sub in ("db", "photos/originals", "photos/display", "generated", "scans"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


SCHEMA = [
    # v1
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin', 'full', 'read_only')),
        password_hash TEXT NOT NULL,
        must_change_password INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        last_login TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );
    CREATE TABLE sites (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL
    );
    CREATE TABLE departments (
        id INTEGER PRIMARY KEY,
        site_id INTEGER NOT NULL REFERENCES sites(id),
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        UNIQUE (site_id, code)
    );
    CREATE TABLE assets (
        id INTEGER PRIMARY KEY,
        site_id INTEGER NOT NULL REFERENCES sites(id),
        department_id INTEGER NOT NULL REFERENCES departments(id),
        asset_number TEXT NOT NULL,
        machine_type TEXT NOT NULL,
        oem TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        serial TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (site_id, asset_number COLLATE NOCASE)
    );
    CREATE TABLE procedures (
        id INTEGER PRIMARY KEY,
        asset_id INTEGER NOT NULL UNIQUE REFERENCES assets(id),
        number_base TEXT NOT NULL UNIQUE,
        copied_from_revision_id INTEGER REFERENCES revisions(id),
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE revisions (
        id INTEGER PRIMARY KEY,
        procedure_id INTEGER NOT NULL REFERENCES procedures(id),
        rev_no INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        version INTEGER NOT NULL DEFAULT 1,
        tasks_covered TEXT NOT NULL DEFAULT '[]',
        affected_employees TEXT NOT NULL DEFAULT '[]',
        shutdown_procedure TEXT NOT NULL DEFAULT '',
        isolate_text TEXT NOT NULL DEFAULT '',
        lockout_devices TEXT NOT NULL DEFAULT '[]',
        group_lockout TEXT NOT NULL DEFAULT '',
        stored_energy TEXT NOT NULL DEFAULT '',
        verify_isolation TEXT NOT NULL DEFAULT '',
        servicing_notes TEXT NOT NULL DEFAULT '',
        restart_steps TEXT NOT NULL DEFAULT '[]',
        locks_required_override INTEGER,
        locks_override_reason TEXT NOT NULL DEFAULT '',
        effective_date TEXT,
        review_date TEXT,
        review_date_manual INTEGER NOT NULL DEFAULT 0,
        document_owner TEXT NOT NULL DEFAULT '',
        change_reason TEXT NOT NULL DEFAULT '',
        template_version TEXT NOT NULL DEFAULT 'EHS-003a_v1',
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (procedure_id, rev_no)
    );
    CREATE TABLE points (
        id INTEGER PRIMARY KEY,
        revision_id INTEGER NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        label TEXT NOT NULL DEFAULT '',
        energy_type TEXT NOT NULL DEFAULT '',
        magnitude TEXT NOT NULL DEFAULT '',
        device TEXT NOT NULL DEFAULT '',
        deenergize TEXT NOT NULL DEFAULT '',
        lock TEXT NOT NULL DEFAULT '',
        stored_energy TEXT NOT NULL DEFAULT '',
        verify TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        locks INTEGER NOT NULL DEFAULT 1,
        photo_missing_reason TEXT NOT NULL DEFAULT '',
        copy_status TEXT NOT NULL DEFAULT 'n/a',
        copy_flags TEXT NOT NULL DEFAULT '{}',
        checked_by INTEGER REFERENCES users(id),
        checked_at TEXT
    );
    CREATE TABLE photos (
        id INTEGER PRIMARY KEY,
        point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        original_name TEXT NOT NULL DEFAULT '',
        width INTEGER, height INTEGER,
        copied_from_photo_id INTEGER,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE generated (
        id INTEGER PRIMARY KEY,
        revision_id INTEGER NOT NULL REFERENCES revisions(id),
        status_at_print TEXT NOT NULL,
        docx_file TEXT NOT NULL,
        pdf_file TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL DEFAULT (datetime('now')),
        user_id INTEGER REFERENCES users(id),
        entity TEXT NOT NULL,
        entity_id INTEGER,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_points_revision ON points(revision_id, seq);
    CREATE INDEX idx_photos_point ON photos(point_id, seq);
    CREATE INDEX idx_revisions_procedure ON revisions(procedure_id, rev_no);
    """,
    # v2: offline checkout, photo markup, temp-id mapping for offline sync
    """
    ALTER TABLE revisions ADD COLUMN checkout_user_id INTEGER REFERENCES users(id);
    ALTER TABLE revisions ADD COLUMN checkout_device TEXT;
    ALTER TABLE revisions ADD COLUMN checkout_device_name TEXT;
    ALTER TABLE revisions ADD COLUMN checkout_at TEXT;
    ALTER TABLE revisions ADD COLUMN checkout_expires TEXT;
    ALTER TABLE photos ADD COLUMN annotations TEXT NOT NULL DEFAULT '[]';
    CREATE TABLE temp_ids (
        device_id TEXT NOT NULL,
        temp_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        real_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (device_id, temp_id)
    );
    """,
    # v3: review / approval workflow, scans, periodic reviews, revision flags
    """
    ALTER TABLE revisions ADD COLUMN printed_for_review_at TEXT;
    ALTER TABLE revisions ADD COLUMN returned_note TEXT NOT NULL DEFAULT '';
    ALTER TABLE revisions ADD COLUMN approved_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE revisions ADD COLUMN approved_date TEXT;
    ALTER TABLE revisions ADD COLUMN approval_note TEXT NOT NULL DEFAULT '';
    ALTER TABLE revisions ADD COLUMN approval_recorded_by INTEGER REFERENCES users(id);
    ALTER TABLE revisions ADD COLUMN activated_at TEXT;
    ALTER TABLE revisions ADD COLUMN activated_by INTEGER REFERENCES users(id);
    ALTER TABLE revisions ADD COLUMN superseded_at TEXT;
    ALTER TABLE procedures ADD COLUMN flag_reason TEXT NOT NULL DEFAULT '';
    ALTER TABLE procedures ADD COLUMN flagged_by INTEGER REFERENCES users(id);
    ALTER TABLE procedures ADD COLUMN flagged_at TEXT;
    ALTER TABLE procedures ADD COLUMN source_ack_rev INTEGER;
    CREATE TABLE scans (
        id INTEGER PRIMARY KEY,
        revision_id INTEGER NOT NULL REFERENCES revisions(id),
        kind TEXT NOT NULL CHECK (kind IN ('markup', 'approval', 'other')),
        file_id TEXT NOT NULL,
        original_name TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        uploaded_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE inspections (
        id INTEGER PRIMARY KEY,
        procedure_id INTEGER NOT NULL REFERENCES procedures(id),
        revision_id INTEGER NOT NULL REFERENCES revisions(id),
        inspected_date TEXT NOT NULL,
        inspector TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        recorded_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_scans_revision ON scans(revision_id);
    CREATE INDEX idx_inspections_procedure ON inspections(procedure_id, inspected_date);
    """,
]

SEED_SITES = [("02", "Villa Rica")]
SEED_DEPARTMENTS = [("02", "PROD", "Production"), ("02", "WH", "Warehouse"), ("02", "FAC", "Facility")]
DEFAULT_ADMIN_PASSWORD = "Password1"


def connect(path=None):
    path = path or data_dir() / "db" / "lotowriter.sqlite3"
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn):
    """Apply schema versions not yet applied; seed starter data on first run."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, ddl in enumerate(SCHEMA[current:], start=current + 1):
        conn.execute("BEGIN")  # executescript() would auto-commit, so run statements one by one
        for stmt in [s for s in ddl.split(";") if s.strip()]:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    if current == 0:
        seed(conn)


def seed(conn):
    from .auth import hash_password
    conn.execute("BEGIN")
    for code, name in SEED_SITES:
        conn.execute("INSERT OR IGNORE INTO sites(code, name) VALUES (?, ?)", (code, name))
    for site_code, code, name in SEED_DEPARTMENTS:
        conn.execute("INSERT OR IGNORE INTO departments(site_id, code, name) "
                     "SELECT id, ?, ? FROM sites WHERE code = ?", (code, name, site_code))
    conn.execute("INSERT OR IGNORE INTO users(username, display_name, role, password_hash, must_change_password) "
                 "VALUES ('admin', 'Administrator', 'admin', ?, 1)", (hash_password(DEFAULT_ADMIN_PASSWORD),))
    conn.execute("COMMIT")
