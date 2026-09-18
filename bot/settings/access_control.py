"""
Bot-wide access control, managed by the owner through her admin panel
(not hardcoded in .env, so she can change it anytime without a redeploy).

- mode: "public" (anyone can use the bot) or "private" (only the owner,
  developer admins, and users on the allow-list can use it)
- force_join_channel: optional @channel_username users must join first
- allowed_users: the allow-list used when mode == "private"
- known_users: every user who has ever started the bot, for the stats view
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH, DATA_DIR, OWNER_USER_ID, ADMIN_USER_IDS


def _connect() -> sqlite3.Connection:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS access_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mode TEXT NOT NULL DEFAULT 'public',
            force_join_channel TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        "INSERT OR IGNORE INTO access_control (id, mode, force_join_channel) VALUES (1, 'public', '')"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS allowed_users (
            user_id INTEGER PRIMARY KEY,
            added_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS known_users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS downloads_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            at TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def record_known_user(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO known_users (user_id, first_seen) VALUES (?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def known_user_count() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM known_users").fetchone()[0]
    finally:
        conn.close()


def get_mode() -> str:
    conn = _connect()
    try:
        return conn.execute("SELECT mode FROM access_control WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def set_mode(mode: str) -> None:
    assert mode in ("public", "private")
    conn = _connect()
    try:
        conn.execute("UPDATE access_control SET mode = ? WHERE id = 1", (mode,))
        conn.commit()
    finally:
        conn.close()


def get_force_join_channel() -> str:
    conn = _connect()
    try:
        return conn.execute("SELECT force_join_channel FROM access_control WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def set_force_join_channel(channel: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE access_control SET force_join_channel = ? WHERE id = 1", (channel,))
        conn.commit()
    finally:
        conn.close()


def list_allowed_users() -> list[int]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT user_id FROM allowed_users ORDER BY added_at").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def add_allowed_user(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, added_at) VALUES (?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def remove_allowed_user(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def list_known_users(limit: int = 50) -> list[tuple[int, str]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, first_seen FROM known_users ORDER BY first_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


def log_download(user_id: int, url: str, status: str) -> None:
    """status: 'success' or 'failed'. Powers the owner's activity feed."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO downloads_log (user_id, url, status, at) VALUES (?, ?, ?, ?)",
            (user_id, url[:300], status, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def download_stats() -> tuple[int, int, int]:
    """Returns (total, success, failed)."""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM downloads_log").fetchone()[0]
        success = conn.execute("SELECT COUNT(*) FROM downloads_log WHERE status = 'success'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM downloads_log WHERE status = 'failed'").fetchone()[0]
        return total, success, failed
    finally:
        conn.close()


def recent_downloads(limit: int = 10) -> list[tuple[int, str, str, str]]:
    """Returns (user_id, url, status, at) tuples, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, url, status, at FROM downloads_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    finally:
        conn.close()


def is_privileged(user_id: int) -> bool:
    """Owner and developer admins always have full access, regardless of
    mode, and can't be locked out by their own settings changes."""
    return user_id == OWNER_USER_ID or user_id in ADMIN_USER_IDS


def is_allowed(user_id: int) -> bool:
    if is_privileged(user_id):
        return True
    if get_mode() == "public":
        return True
    return user_id in list_allowed_users()
