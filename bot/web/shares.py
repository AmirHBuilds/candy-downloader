"""
Built-in "Get a link": a finished file is copied into data/shares/<token>/ and served by the web app itself at
/s/<token> (a small landing page) and /s/<token>/<name> (the file). Nobody needs an account to open a link, so the
token is the secret: 256 random bits, and unknown / expired / used-up links all look the same (404).

Used by both the web app and the bot, so the files are owned by `user_id` (Telegram id, or -account.id for web-only
accounts) and one person sees the same links in both places.
"""
import re
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from web import security

import config as app_config
from config import DB_PATH, DATA_DIR

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
STORED_NAME = "file"
ANYONE, LOGIN, OWNER = 0, 1, 2          # who may open a link: anyone with it / any signed-in web account / only its maker
ACCESS_NAMES = {ANYONE: "anyone with the link", LOGIN: "signed-in people only", OWNER: "only me"}
DEFAULTS = {"enabled": True, "default_hours": 72, "max_hours": 720, "user_quota_mb": 2048,
            "default_access": ANYONE, "min_access": ANYONE}        # min_access: the admin can forbid fully public links
LIMITS = {"default_hours": (1, 8760), "max_hours": (1, 8760), "user_quota_mb": (1, 1_000_000)}


class ShareError(Exception):
    """User-facing, plain text."""


@dataclass
class Share:
    id: int
    token: str
    owner: int
    name: str
    size: int
    created: float
    expires: float
    downloads: int
    max_downloads: int
    access: int = ANYONE
    pw_hash: str = ""

    def path(self) -> Path:
        return share_root() / self.token / STORED_NAME

    def alive(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.expires > now and not (self.max_downloads and self.downloads >= self.max_downloads)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size, "created": self.created, "expires": self.expires,
                "downloads": self.downloads, "max_downloads": self.max_downloads, "access": self.access,
                "has_password": bool(self.pw_hash), "path": f"/s/{self.token}", "direct": f"/s/{self.token}/{quote(self.name)}"}


def share_root() -> Path:
    return Path(DATA_DIR) / "shares"


def _connect() -> sqlite3.Connection:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS web_shares (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, "
                 "owner INTEGER NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL, "
                 "expires REAL NOT NULL, downloads INTEGER NOT NULL DEFAULT 0, max_downloads INTEGER NOT NULL DEFAULT 0)")
    have = {row[1] for row in conn.execute("PRAGMA table_info(web_shares)")}
    if "access" not in have:
        conn.execute("ALTER TABLE web_shares ADD COLUMN access INTEGER NOT NULL DEFAULT 0")
    if "pw_hash" not in have:
        conn.execute("ALTER TABLE web_shares ADD COLUMN pw_hash TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE TABLE IF NOT EXISTS share_config (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.commit()
    return conn


_COLS = "id, token, owner, name, size, created, expires, downloads, max_downloads, access, pw_hash"


# ------------------------------------------------------------------ settings (admin)
def get_config() -> dict:
    conn = _connect()
    try:
        rows = dict(conn.execute("SELECT k, v FROM share_config").fetchall())
    finally:
        conn.close()
    result = dict(DEFAULTS)
    if "enabled" in rows:
        result["enabled"] = rows["enabled"] == "1"
    for key in LIMITS:
        if key in rows and rows[key].isdigit():
            result[key] = int(rows[key])
    for key in ("default_access", "min_access"):
        if key in rows and rows[key] in ("0", "1", "2"):
            result[key] = int(rows[key])
    result["default_hours"] = min(result["default_hours"], result["max_hours"])
    result["default_access"] = max(result["default_access"], result["min_access"])
    return result


def set_config(data: dict) -> dict:
    updates = {}
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ShareError("Enabled must be on or off.")
        updates["enabled"] = "1" if data["enabled"] else "0"
    for key, (low, high) in LIMITS.items():
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ShareError(f"{key.replace('_', ' ')} must be a whole number from {low} to {high}.")
            updates[key] = str(value)
    for key in ("default_access", "min_access"):
        if key in data:
            if isinstance(data[key], bool) or data[key] not in (ANYONE, LOGIN, OWNER):
                raise ShareError("Who can open a link: 0 anyone, 1 signed-in people, 2 only the maker.")
            updates[key] = str(data[key])
    conn = _connect()
    try:
        for key, value in updates.items():
            conn.execute("INSERT INTO share_config (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                         (key, value))
        conn.commit()
    finally:
        conn.close()
    return get_config()


def available() -> bool:
    """Links only work while the web server runs, and only if an admin hasn't switched them off."""
    return bool(app_config.WEB_ENABLED) and get_config()["enabled"]


def base_url() -> str:
    return app_config.WEB_PUBLIC_URL or f"http://localhost:{app_config.WEB_PORT}"


def absolute(share: Share) -> str:
    return f"{base_url()}/s/{share.token}"


# ------------------------------------------------------------------ the shares
def _row(row) -> Share:
    return Share(*row)


def usage(owner: int) -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COALESCE(SUM(size), 0) FROM web_shares WHERE owner = ?", (owner,)).fetchone()[0]
    finally:
        conn.close()


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(src, dest)
    except BaseException:
        shutil.rmtree(dest.parent, ignore_errors=True)
        raise


def _clean_access(value, cfg: dict) -> int:
    if value is None:
        value = cfg["default_access"]
    if isinstance(value, bool) or value not in (ANYONE, LOGIN, OWNER):
        raise ShareError("Pick who may open the link.")
    return max(value, cfg["min_access"])


def _clean_password(password) -> str:
    if password in (None, ""):
        return ""
    if not isinstance(password, str) or not 4 <= len(password) <= 100:
        raise ShareError("A link password needs 4 to 100 characters.")
    return security.hash_password(password)


def create(owner: int, src: Path, name: str, hours: int | None = None, max_downloads: int = 0,
           access: int | None = None, password: str = "") -> Share:
    """Copy the file into the share folder and register the link. Blocking: call it from a thread."""
    cfg = get_config()
    if not cfg["enabled"]:
        raise ShareError("Links are switched off by the admin.")
    src = Path(src)
    if not src.is_file():
        raise ShareError("That file isn't available any more.")
    hours = cfg["default_hours"] if hours is None else hours
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
        raise ShareError("Pick how long the link should last.")
    hours = min(hours, cfg["max_hours"])
    if isinstance(max_downloads, bool) or not isinstance(max_downloads, int) or not 0 <= max_downloads <= 1000:
        raise ShareError("The download limit is 0 (no limit) to 1000.")
    access = _clean_access(access, cfg)
    pw_hash = _clean_password(password)
    size = src.stat().st_size
    if usage(owner) + size > cfg["user_quota_mb"] * 1_000_000:
        raise ShareError(f"That would go over your {cfg['user_quota_mb']} MB of shared files. Delete an old link first.")
    token = secrets.token_urlsafe(32)
    clean = (name or "file").replace("\x00", "").replace("/", "_").replace("\\", "_").strip()[:180] or "file"
    _copy(src, share_root() / token / STORED_NAME)
    now = time.time()
    conn = _connect()
    try:
        cur = conn.execute("INSERT INTO web_shares (token, owner, name, size, created, expires, downloads, max_downloads, "
                           "access, pw_hash) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                           (token, owner, clean, size, now, now + hours * 3600, max_downloads, access, pw_hash))
        conn.commit()
        row_id = cur.lastrowid
    except BaseException:
        shutil.rmtree(share_root() / token, ignore_errors=True)
        raise
    finally:
        conn.close()
    return Share(row_id, token, owner, clean, size, now, now + hours * 3600, 0, max_downloads, access, pw_hash)


def find(token: str) -> Share | None:
    """The live share for this token, or None (also for malformed, expired and used-up tokens)."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    conn = _connect()
    try:
        row = conn.execute(f"SELECT {_COLS} FROM web_shares WHERE token = ?", (token,)).fetchone()
    finally:
        conn.close()
    share = _row(row) if row else None
    return share if share and share.alive() and share.path().is_file() else None


def count_download(share_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE web_shares SET downloads = downloads + 1 WHERE id = ?", (share_id,))
        conn.commit()
    finally:
        conn.close()


def list_for(owner: int | None = None) -> list[Share]:
    """Live shares, newest first; owner None = everyone's (admin)."""
    conn = _connect()
    try:
        if owner is None:
            rows = conn.execute(f"SELECT {_COLS} FROM web_shares ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(f"SELECT {_COLS} FROM web_shares WHERE owner = ? ORDER BY id DESC", (owner,)).fetchall()
    finally:
        conn.close()
    return [s for s in map(_row, rows) if s.alive()]


def get(share_id: int) -> Share | None:
    conn = _connect()
    try:
        row = conn.execute(f"SELECT {_COLS} FROM web_shares WHERE id = ?", (share_id,)).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def update(share_id: int, owner: int | None, changes: dict) -> Share | None:
    """Change who may open a link, its download limit, its password or its lifetime (counted from now).
    With `owner`, only that person's own link. Returns the link, or None when it isn't theirs / isn't there."""
    share = get(share_id)
    if share is None or (owner is not None and share.owner != owner) or not share.alive():
        return None
    cfg = get_config()
    sets, values = [], []
    if "access" in changes:
        sets.append("access = ?")
        values.append(_clean_access(changes["access"], cfg))
    if "max_downloads" in changes:
        value = changes["max_downloads"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
            raise ShareError("The download limit is 0 (no limit) to 1000.")
        sets.append("max_downloads = ?")
        values.append(value)
    if "hours" in changes:
        hours = changes["hours"]
        if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
            raise ShareError("Pick how long the link should last.")
        sets.append("expires = ?")
        values.append(time.time() + min(hours, cfg["max_hours"]) * 3600)
    if changes.get("clear_password"):
        sets.append("pw_hash = ''")
    elif changes.get("password"):
        sets.append("pw_hash = ?")
        values.append(_clean_password(changes["password"]))
    if sets:
        conn = _connect()
        try:
            conn.execute(f"UPDATE web_shares SET {', '.join(sets)} WHERE id = ?", (*values, share_id))
            conn.commit()
        finally:
            conn.close()
    return get(share_id)


def permits(share: Share, account) -> bool:
    """May this viewer (a signed-in Account, or None) open the link? The password, if any, is a separate step."""
    if share.access == ANYONE:
        return True
    if account is None:
        return False
    return share.access == LOGIN or (share.access == OWNER and account.user_id == share.owner)


def check_password(share: Share, password: str) -> bool:
    if not share.pw_hash:
        return True
    return security.verify_password(password or "", share.pw_hash)


def delete(share_id: int, owner: int | None = None) -> bool:
    """Remove a link and its file. With `owner`, only that person's own link."""
    share = get(share_id)
    if share is None or (owner is not None and share.owner != owner):
        return False
    conn = _connect()
    try:
        conn.execute("DELETE FROM web_shares WHERE id = ?", (share_id,))
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(share_root() / share.token, ignore_errors=True)
    return True


def delete_owner(owner: int) -> int:
    count = 0
    for share in list_for(owner):
        count += delete(share.id)
    return count


def sweep() -> int:
    """Drop expired / used-up links, and folders that no link points to. Returns how many links were removed."""
    conn = _connect()
    try:
        rows = [_row(r) for r in conn.execute(f"SELECT {_COLS} FROM web_shares").fetchall()]
    finally:
        conn.close()
    removed = 0
    for share in rows:
        if not share.alive() or not share.path().is_file():
            removed += delete(share.id)
    known = {s.token for s in rows}
    root = share_root()
    if root.is_dir():
        for folder in root.iterdir():
            if folder.name not in known and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
    return removed
