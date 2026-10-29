"""
User accounts - each user's data and settings are their own.

* Accounts: email + name + password. Passwords are never stored: only a salted PBKDF2-HMAC-SHA256 hash
  (240,000 iterations, 16-byte random salt per user), compared in constant time.
* Sessions: a random 256-bit token in an HttpOnly cookie (SameSite=Lax, 30 days). Only the token's SHA-256 is
  stored, so a copy of the database cannot be used to log in.
* Repeated wrong passwords: after 5 failures for one email within 10 minutes, logging in is paused for that email.
* Per-user data: the logged-in user is kept in a context variable for the request; history_service stores
  battery readings and trips under the user's own vehicle id, so no user can read another user's data.
* Preferences (theme, vehicle profile, live updates) are stored per user and follow them to any browser.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3  # noqa: F401  (local file when DATABASE_URL is not set - see db.py)
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import db

DB = Path(__file__).resolve().parent.parent / "data" / "records.db"
COOKIE = "evem_session"
SESSION_DAYS = 30
ITERATIONS = 240_000
MAX_FAILS, FAIL_WINDOW_S = 5, 600
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
THEMES = ("system", "light", "dark")
current_user: ContextVar = ContextVar("current_user", default=None)
_lock = threading.Lock()
_fails = {}


class AuthError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _conn():
    c = db.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT, pw_hash TEXT NOT NULL,
        salt TEXT NOT NULL, created_utc TEXT NOT NULL, preferences TEXT DEFAULT '{}')""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_utc TEXT NOT NULL, expires_utc TEXT NOT NULL)""")
    return c


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _public(row):
    try:
        prefs = json.loads(row["preferences"] or "{}")
    except ValueError:
        prefs = {}
    return {"id": row["id"], "email": row["email"], "name": row["name"], "preferences": prefs,
            "vehicle_id": vehicle_id_for(row["id"])}


def vehicle_id_for(user_id):
    return f"user-{user_id}"


def register(email, name, password):
    email = (email or "").strip().lower()
    name = " ".join((name or "").split())[:60]
    if not EMAIL.match(email):
        raise AuthError("Enter a valid email address.")
    if not name:
        raise AuthError("Enter your name.")
    if len(password or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")
    if len(password) > 200:
        raise AuthError("Password is too long.")
    salt = secrets.token_hex(16)
    with _lock, _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError("An account with this email already exists – log in instead.")
        cur = c.execute("INSERT INTO users (email, name, pw_hash, salt, created_utc) VALUES (?, ?, ?, ?, ?)",
                        (email, name, _hash(password, salt), salt, _now().isoformat()))
        row = c.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _public(row)


def ensure_account(email, name, password):
    """Make sure this account exists with this password (used for the demo account on a host whose database is
    wiped on every restart). Returns the account, or None when the details are not usable."""
    email = (email or "").strip().lower()
    if not EMAIL.match(email) or len(password or "") < 8:
        return None
    try:
        return register(email, name or "Demo", password)
    except AuthError:                                  # already there: keep it, with this password
        salt = secrets.token_hex(16)
        with _lock, _conn() as c:
            c.execute("UPDATE users SET pw_hash = ?, salt = ? WHERE email = ?", (_hash(password, salt), salt, email))
            row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _public(row) if row else None


def login(email, password):
    """-> (user, session token). Raises AuthError on wrong details."""
    email = (email or "").strip().lower()
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(email, []) if now - t < FAIL_WINDOW_S]
        _fails[email] = recent
    if len(recent) >= MAX_FAILS:
        wait = int(FAIL_WINDOW_S - (now - recent[0])) // 60 + 1
        raise AuthError(f"Too many wrong attempts – try again in about {wait} minute{'s' if wait != 1 else ''}.")
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # hash even when the email is unknown, so the reply takes the same time either way
    ok = hmac.compare_digest(_hash(password or "", row["salt"] if row else "00" * 16),
                             row["pw_hash"] if row else "x")
    if not row or not ok:
        with _lock:
            _fails.setdefault(email, []).append(now)
        raise AuthError("Email or password is not correct.")
    with _lock:
        _fails.pop(email, None)
    return _public(row), new_session(row["id"])


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    with _lock, _conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_utc < ?", (_now().isoformat(),))
        c.execute("INSERT INTO sessions (token_hash, user_id, created_utc, expires_utc) VALUES (?, ?, ?, ?)",
                  (_token_hash(token), user_id, _now().isoformat(),
                   (_now() + timedelta(days=SESSION_DAYS)).isoformat()))
    return token


def session_user(token):
    if not token or len(token) > 100:
        return None
    with _lock, _conn() as c:
        row = c.execute("SELECT u.*, s.expires_utc FROM sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash = ?", (_token_hash(token),)).fetchone()
    if not row or datetime.fromisoformat(row["expires_utc"]) < _now():
        return None
    return _public(row)


def logout(token):
    if token:
        with _lock, _conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def set_preferences(user_id, updates):
    if not isinstance(updates, dict):
        raise AuthError("Preferences must be an object.")
    if "theme" in updates and updates["theme"] not in THEMES:
        raise AuthError("Theme must be system, light or dark.")
    allowed = {"theme", "profile", "auto"}
    with _lock, _conn() as c:
        row = c.execute("SELECT preferences FROM users WHERE id = ?", (user_id,)).fetchone()
        prefs = json.loads(row["preferences"] or "{}") if row else {}
        prefs.update({k: v for k, v in updates.items() if k in allowed})
        text = json.dumps(prefs)
        if len(text) > 20_000:
            raise AuthError("Preferences are too large.")
        c.execute("UPDATE users SET preferences = ? WHERE id = ?", (text, user_id))
    return prefs
