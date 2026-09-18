"""
Per-user settings, stored in SQLite (tiny, survives restarts, never grows
large - just one row per user who has ever changed a default).

DEFAULTS defines every configurable knob CandyDownloader exposes. Any field
not customized by a user simply falls back to these.
"""
import json
import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH, DATA_DIR

DEFAULTS: dict[str, Any] = {
    # --- Video/quality ---
    "mode": "video",                 # video | audio
    "quality": "best",               # best | worst | 1080p | 720p | 480p | 360p
    "video_codec": "any",            # any | h264 | vp9 | av1

    # --- Audio extraction (used when mode == "audio") ---
    "audio_format": "mp3",           # mp3 | m4a | opus | flac | wav
    "audio_bitrate": "192",          # kbps, as string for yt-dlp postprocessor args

    # --- Subtitles ---
    "subtitles": "off",              # off | auto | manual
    "subtitle_langs": "en",          # comma separated language codes
    "embed_subtitles": False,

    # --- Playlists ---
    "playlist_mode": "single",       # single | full | range
    "playlist_range": "",            # e.g. "1-5" used when playlist_mode == range

    # --- Extras ---
    "embed_thumbnail": True,
    "embed_metadata": True,
    "sponsorblock": False,           # auto-skip sponsor segments (YouTube)
    "use_archive": False,            # skip re-downloading items already sent before

    # --- Network / performance ---
    "rate_limit_kbps": 0,            # 0 = unlimited
    "concurrent_fragments": 4,
    "proxy": "",                     # e.g. socks5://127.0.0.1:1080, blank = none

    # --- Filenames ---
    "filename_template": "%(title)s.%(ext)s",

    # --- Auth ---
    "cookies_enabled": False,        # use this user's uploaded cookies file, if any
}


def _connect() -> sqlite3.Connection:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            settings_json TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def get_settings(user_id: int) -> dict[str, Any]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT settings_json FROM user_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        merged = dict(DEFAULTS)
        if row:
            merged.update(json.loads(row[0]))
        return merged
    finally:
        conn.close()


def update_setting(user_id: int, key: str, value: Any) -> dict[str, Any]:
    if key not in DEFAULTS:
        raise KeyError(f"Unknown setting: {key}")
    current = get_settings(user_id)
    current[key] = value
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO user_settings (user_id, settings_json) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json""",
            (user_id, json.dumps(current)),
        )
        conn.commit()
    finally:
        conn.close()
    return current


def reset_settings(user_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
