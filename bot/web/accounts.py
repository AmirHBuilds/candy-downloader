"""
Web accounts and sessions (SQLite, the same database file as the rest of the bot).

An account may be linked to a Telegram user id. A linked account IS that Telegram user as far as settings, cookies and
history go (same person, same preferences in the bot and on the web). An unlinked account gets a negative internal user
id (-account id), which can never clash with a Telegram id.

Nothing here knows about HTTP; the app layer calls these in a thread (sqlite is blocking).
"""
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from config import DB_PATH, DATA_DIR
from web import security

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
ROLES = ("user", "admin")

SESSION_IDLE_SECONDS = 14 * 24 * 3600          # unused for two weeks -> logged out
SESSION_MAX_SECONDS = 30 * 24 * 3600           # never longer than a month
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
MAX_SESSIONS_PER_ACCOUNT = 10


class AccountError(ValueError):
    """Something the person (or admin) did wrong; the message is written for them (plain text)."""


@dataclass
class Account:
    id: int
    username: str
    role: str
    telegram_id: int | None
    disabled: bool
    must_change: bool
    created_at: float
    last_login: float | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def user_id(self) -> int:
        """The id settings / cookies / history are stored under."""
        return self.telegram_id if self.telegram_id is not None else -self.id

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "telegram_id": self.telegram_id,
                "disabled": self.disabled, "must_change": self.must_change, "created_at": self.created_at,
                "last_login": self.last_login}


_COLUMNS = "id, username, role, telegram_id, disabled, must_change, created_at, last_login"


def _account(row) -> Account:
    return Account(row[0], row[1], row[2], row[3], bool(row[4]), bool(row[5]), row[6], row[7])


def _connect() -> sqlite3.Connection:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS web_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        telegram_id INTEGER UNIQUE,
        disabled INTEGER NOT NULL DEFAULT 0,
        must_change INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_login REAL,
        failed_count INTEGER NOT NULL DEFAULT 0,
        locked_until REAL NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
        token_hash TEXT PRIMARY KEY,
        account_id INTEGER NOT NULL,
        csrf TEXT NOT NULL,
        created REAL NOT NULL,
        last_seen REAL NOT NULL,
        ip TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT '')""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_account ON web_sessions(account_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS web_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, account_id INTEGER, action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '')""")
    conn.commit()
    return conn


# ------------------------------------------------------------------ audit
def audit(action: str, account_id: int | None = None, detail: str = "", ip: str = "") -> None:
    conn = _connect()
    try:
        conn.execute("INSERT INTO web_audit (at, account_id, action, detail, ip) VALUES (?,?,?,?,?)",
                     (time.time(), account_id, action, detail[:300], ip[:64]))
        conn.execute("DELETE FROM web_audit WHERE id <= (SELECT MAX(id) FROM web_audit) - 2000")
        conn.commit()
    finally:
        conn.close()


def recent_audit(limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("""SELECT a.at, COALESCE(w.username, ''), a.action, a.detail, a.ip
                               FROM web_audit a LEFT JOIN web_accounts w ON w.id = a.account_id
                               ORDER BY a.id DESC LIMIT ?""", (limit,)).fetchall()
        return [{"at": r[0], "username": r[1], "action": r[2], "detail": r[3], "ip": r[4]} for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------ accounts
def check_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AccountError("Usernames are 3-32 characters: letters, digits, dot, dash or underscore.")
    return username


def create_account(username: str, password: str, role: str = "user", telegram_id: int | None = None,
                   must_change: bool = False) -> Account:
    username = check_username(username)
    if role not in ROLES:
        raise AccountError("Unknown role.")
    problem = security.check_password_policy(password, username)
    if problem:
        raise AccountError(problem)
    pw_hash = security.hash_password(password)
    conn = _connect()
    try:
        try:
            cursor = conn.execute(
                "INSERT INTO web_accounts (username, pw_hash, role, telegram_id, must_change, created_at) "
                "VALUES (?,?,?,?,?,?)", (username, pw_hash, role, telegram_id, int(must_change), time.time()))
            conn.commit()
        except sqlite3.IntegrityError:
            taken = conn.execute("SELECT 1 FROM web_accounts WHERE username = ?", (username,)).fetchone()
            raise AccountError("That username is taken." if taken else "That Telegram user already has an account.")
        return _get(conn, cursor.lastrowid)
    finally:
        conn.close()


def _get(conn, account_id: int) -> Account | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM web_accounts WHERE id = ?", (account_id,)).fetchone()
    return _account(row) if row else None


def get_account(account_id: int) -> Account | None:
    conn = _connect()
    try:
        return _get(conn, account_id)
    finally:
        conn.close()


def account_by_telegram(telegram_id: int) -> Account | None:
    conn = _connect()
    try:
        row = conn.execute(f"SELECT {_COLUMNS} FROM web_accounts WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return _account(row) if row else None
    finally:
        conn.close()


def account_by_user_id(user_id: int) -> Account | None:
    """The account whose settings/files live under this internal user id."""
    return account_by_telegram(user_id) if user_id > 0 else get_account(-user_id)


def list_accounts() -> list[Account]:
    conn = _connect()
    try:
        return [_account(r) for r in conn.execute(f"SELECT {_COLUMNS} FROM web_accounts ORDER BY id")]
    finally:
        conn.close()


def _enabled_admins(conn, excluding: int | None = None) -> int:
    return conn.execute("SELECT COUNT(*) FROM web_accounts WHERE role='admin' AND disabled=0 AND id != ?",
                        (excluding or 0,)).fetchone()[0]


def _revoke(conn, account_id: int) -> None:
    conn.execute("DELETE FROM web_sessions WHERE account_id = ?", (account_id,))


def set_password(account_id: int, password: str, must_change: bool = False) -> None:
    account = get_account(account_id)
    if account is None:
        raise AccountError("No such account.")
    problem = security.check_password_policy(password, account.username)
    if problem:
        raise AccountError(problem)
    pw_hash = security.hash_password(password)
    conn = _connect()
    try:
        conn.execute("UPDATE web_accounts SET pw_hash=?, must_change=?, failed_count=0, locked_until=0 WHERE id=?",
                     (pw_hash, int(must_change), account_id))
        _revoke(conn, account_id)          # a new password signs every device out
        conn.commit()
    finally:
        conn.close()


def set_role(account_id: int, role: str) -> None:
    if role not in ROLES:
        raise AccountError("Unknown role.")
    conn = _connect()
    try:
        if role != "admin" and _enabled_admins(conn, excluding=account_id) == 0:
            raise AccountError("There must always be at least one active admin.")
        conn.execute("UPDATE web_accounts SET role=? WHERE id=?", (role, account_id))
        conn.commit()
    finally:
        conn.close()


def set_disabled(account_id: int, disabled: bool) -> None:
    conn = _connect()
    try:
        if disabled and _enabled_admins(conn, excluding=account_id) == 0:
            row = conn.execute("SELECT role FROM web_accounts WHERE id=?", (account_id,)).fetchone()
            if row and row[0] == "admin":
                raise AccountError("There must always be at least one active admin.")
        conn.execute("UPDATE web_accounts SET disabled=? WHERE id=?", (int(disabled), account_id))
        if disabled:
            _revoke(conn, account_id)
        conn.commit()
    finally:
        conn.close()


def delete_account(account_id: int) -> None:
    conn = _connect()
    try:
        row = conn.execute("SELECT role, disabled FROM web_accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise AccountError("No such account.")
        if row[0] == "admin" and not row[1] and _enabled_admins(conn, excluding=account_id) == 0:
            raise AccountError("There must always be at least one active admin.")
        _revoke(conn, account_id)
        conn.execute("DELETE FROM web_accounts WHERE id=?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def ensure_bootstrap_admin(username: str, password: str) -> Account | None:
    """First start: an admin from the .env values, only when no admin exists. Never overwrites anything."""
    if not username or not password:
        return None
    conn = _connect()
    try:
        if conn.execute("SELECT 1 FROM web_accounts WHERE role='admin' LIMIT 1").fetchone():
            return None
    finally:
        conn.close()
    return create_account(username, password, role="admin")


# ------------------------------------------------------------------ logging in
def authenticate(username: str, password: str) -> Account | None:
    """The account when the credentials are right; None otherwise. A wrong guess costs the same time whether the
    username exists or not, and five misses in a row lock the account for a while (even against the right password)."""
    username = (username or "").strip()[:64]
    password = (password or "")[:security.MAX_PASSWORD_LENGTH + 1]
    conn = _connect()
    try:
        row = conn.execute("SELECT id, pw_hash, disabled, failed_count, locked_until FROM web_accounts "
                           "WHERE username = ?", (username,)).fetchone()
        if row is None:
            security.burn_time()
            return None
        account_id, pw_hash, disabled, failed, locked_until = row
        if locked_until > time.time():
            security.burn_time()
            return None
        ok = security.verify_password(password, pw_hash)
        if not ok or disabled:
            if not ok:
                failed += 1
                locked = time.time() + LOCKOUT_SECONDS if failed >= MAX_FAILED_LOGINS else 0
                conn.execute("UPDATE web_accounts SET failed_count=?, locked_until=? WHERE id=?",
                             (0 if locked else failed, locked, account_id))
                conn.commit()
            return None
        conn.execute("UPDATE web_accounts SET failed_count=0, locked_until=0, last_login=? WHERE id=?",
                     (time.time(), account_id))
        conn.commit()
        return _get(conn, account_id)
    finally:
        conn.close()


def is_locked(username: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT locked_until FROM web_accounts WHERE username = ?", (username.strip(),)).fetchone()
        return bool(row and row[0] > time.time())
    finally:
        conn.close()


# ------------------------------------------------------------------ sessions
def create_session(account_id: int, ip: str = "", agent: str = "") -> tuple[str, str]:
    """(token for the cookie, csrf token for the page). Only a hash of the token is stored."""
    token, csrf = security.new_token(), security.new_token()
    now = time.time()
    conn = _connect()
    try:
        conn.execute("INSERT INTO web_sessions (token_hash, account_id, csrf, created, last_seen, ip, agent) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (security.hash_token(token), account_id, csrf, now, now, ip[:64], agent[:200]))
        # keep the newest few: logging in on a tenth device signs the oldest one out
        conn.execute("""DELETE FROM web_sessions WHERE account_id = ? AND token_hash NOT IN (
                        SELECT token_hash FROM web_sessions WHERE account_id = ? ORDER BY created DESC LIMIT ?)""",
                     (account_id, account_id, MAX_SESSIONS_PER_ACCOUNT))
        conn.commit()
    finally:
        conn.close()
    return token, csrf


def session_account(token: str) -> tuple[Account, str] | None:
    """(account, csrf token) for a valid session cookie; None when unknown, expired, or the account is disabled."""
    if not token:
        return None
    now = time.time()
    token_hash = security.hash_token(token)
    conn = _connect()
    try:
        row = conn.execute("SELECT account_id, csrf, created, last_seen FROM web_sessions WHERE token_hash = ?",
                           (token_hash,)).fetchone()
        if row is None:
            return None
        account_id, csrf, created, last_seen = row
        if now - last_seen > SESSION_IDLE_SECONDS or now - created > SESSION_MAX_SECONDS:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
            return None
        account = _get(conn, account_id)
        if account is None or account.disabled:
            conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
            return None
        if now - last_seen > 300:                      # no write on every request
            conn.execute("UPDATE web_sessions SET last_seen = ? WHERE token_hash = ?", (now, token_hash))
            conn.commit()
        return account, csrf
    finally:
        conn.close()


def end_session(token: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (security.hash_token(token),))
        conn.commit()
    finally:
        conn.close()


def end_other_sessions(account_id: int, keep_token: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM web_sessions WHERE account_id = ? AND token_hash != ?",
                     (account_id, security.hash_token(keep_token)))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ for the bot's commands
def username_for_telegram(telegram_id: int, telegram_username: str | None) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "", telegram_username or "") or f"tg{telegram_id}"
    if len(base) < 3:
        base = f"tg_{base}"
    return base[:28]


def issue_for_telegram(telegram_id: int, telegram_username: str | None, role: str = "user") -> tuple[Account, str, bool]:
    """Create (or reset the password of) the account of a Telegram user. (account, one-time password, created).
    The password must be changed at first sign-in. A reset never changes the role or the username."""
    password = security.generate_password()
    existing = account_by_telegram(telegram_id)
    if existing is not None:
        set_password(existing.id, password, must_change=True)
        if existing.disabled:
            set_disabled(existing.id, False)
        return get_account(existing.id), password, False
    base = username_for_telegram(telegram_id, telegram_username)
    for attempt in range(6):
        name = base if attempt == 0 else f"{base[:24]}{security.generate_password(4)}"
        try:
            return create_account(name, password, role=role, telegram_id=telegram_id, must_change=True), password, True
        except AccountError as exc:
            if "taken" not in str(exc):
                raise
    raise AccountError("Couldn't find a free username.")
