"""Passwords, sessions and login rules."""
import base64
import datetime as dt
import hashlib
import hmac
import secrets

SESSION_DAYS = 14
MAX_FAILED = 10
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LEN = 8
ROLES = ("admin", "full", "read_only")

_N, _R, _P = 2 ** 14, 8, 1  # scrypt cost (~16 MB, fast enough for a small server)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def password_problem(password, username=""):
    """Return a reason the password is not acceptable, or None."""
    from .db import DEFAULT_ADMIN_PASSWORD
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if password.lower() == DEFAULT_ADMIN_PASSWORD.lower():
        return "Password can't be the default password."
    if username and password.lower() == username.lower():
        return "Password can't be the same as the user name."
    return None


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _ts(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class LoginError(Exception):
    pass


def login(conn, username, password):
    """Check credentials and return (user_row, session_token). Raises LoginError."""
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    now = _now()
    if user is None:
        verify_password(password, hash_password("x"))  # similar timing for unknown users
        raise LoginError("Incorrect user name or password.")
    if not user["active"]:
        raise LoginError("This account is deactivated. Ask an administrator.")
    if user["locked_until"] and user["locked_until"] > _ts(now):
        raise LoginError(f"Too many failed attempts. Try again after {LOCKOUT_MINUTES} minutes.")
    if not verify_password(password, user["password_hash"]):
        failed = user["failed_attempts"] + 1
        locked = _ts(now + dt.timedelta(minutes=LOCKOUT_MINUTES)) if failed >= MAX_FAILED else None
        conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                     (0 if locked else failed, locked, user["id"]))
        raise LoginError("Incorrect user name or password.")
    conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login = ? WHERE id = ?",
                 (_ts(now), user["id"]))
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (_token_hash(token), user["id"], _ts(now + dt.timedelta(days=SESSION_DAYS))))
    return user, token


def user_for_token(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
        (_token_hash(token), _ts(_now()))).fetchone()
    return row


def logout(conn, token):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def end_sessions(conn, user_id, keep_token=None):
    if keep_token:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _token_hash(keep_token)))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_password(conn, user_id, new_password, must_change):
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = ?, failed_attempts = 0, "
                 "locked_until = NULL WHERE id = ?", (hash_password(new_password), 1 if must_change else 0, user_id))


def active_admin_count(conn):
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]
