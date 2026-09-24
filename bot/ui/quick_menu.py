"""
Menus shown right after a link is detected. Quality buttons are built
from what the link *actually* offers (see downloader/probe.py) rather
than a fixed guess - a 720p-max video only shows 720p and below, and
sites with no real quality concept (galleries, direct files) skip
quality selection entirely.

Every callback_data here ends with a request token (rid) - a short id
unique to *this specific link/message*, not just the user. Without it,
sending a second link before acting on the first would silently make
the first message's buttons act on the second link instead (they'd
share one per-user slot) - this is what threading the token through
everywhere prevents.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.probe import ProbeResult


def cancel_row(rid: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("✕ Cancel", callback_data=f"dl|cancel|{rid}")]


def video_menu(probe: ProbeResult, rid: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("★ Best available", callback_data=f"dl|video|best|{rid}")]]

    shown = [h for h in probe.heights if h]
    picks = []
    for h in shown:
        if not picks or (picks[-1] - h) >= 120:
            picks.append(h)
        if len(picks) == 3:
            break
    if picks:
        rows.append([
            InlineKeyboardButton(f"{h}p", callback_data=f"dl|video|{h}p|{rid}") for h in picks
        ])

    if probe.has_audio:
        rows.append([
            InlineKeyboardButton("♪ MP3", callback_data=f"dl|audio|mp3|{rid}"),
            InlineKeyboardButton("♪ Opus", callback_data=f"dl|audio|opus|{rid}"),
        ])

    rows.append([InlineKeyboardButton("More options…", callback_data=f"dl|moreq|{rid}")])
    rows.append(cancel_row(rid))
    return InlineKeyboardMarkup(rows)


def extended_video_menu(probe: ProbeResult, rid: str) -> InlineKeyboardMarkup:
    rows = []
    picks = []
    for h in probe.heights or []:
        if not picks or (picks[-1] - h) >= 60:
            picks.append(h)
    for i in range(0, len(picks), 3):
        rows.append([
            InlineKeyboardButton(f"{h}p", callback_data=f"dl|video|{h}p|{rid}") for h in picks[i:i + 3]
        ])
    rows.append([InlineKeyboardButton("↓ Smallest size", callback_data=f"dl|video|worst|{rid}")])
    rows.append([InlineKeyboardButton("← Back", callback_data=f"dl|backq|{rid}")])
    rows.append(cancel_row(rid))
    return InlineKeyboardMarkup(rows)


def audio_only_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♪ MP3", callback_data=f"dl|audio|mp3|{rid}"),
         InlineKeyboardButton("♪ Opus", callback_data=f"dl|audio|opus|{rid}")],
        cancel_row(rid),
    ])


def simple_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↓ Download", callback_data=f"dl|simple|download|{rid}")],
        cancel_row(rid),
    ])


def fallback_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("★ Best quality", callback_data=f"dl|video|best|{rid}")],
        [InlineKeyboardButton("↓ Smallest size", callback_data=f"dl|video|worst|{rid}"),
         InlineKeyboardButton("♪ Audio only", callback_data=f"dl|audio|mp3|{rid}")],
        cancel_row(rid),
    ])


def spotify_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♪ Download MP3", callback_data=f"dl|audio|mp3|{rid}")],
        cancel_row(rid),
    ])


def send_as_file_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▤ Send as file instead", callback_data=f"dl|asfile|{rid}")],
    ])


def retry_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↻ Try again", callback_data=f"dl|retry|{rid}")]])


def cancelled_menu(rid: str) -> InlineKeyboardMarkup:
    """Cancelled state: offer both a retry and a clean way to dismiss."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Try again", callback_data=f"dl|retry|{rid}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"dl|dismiss|{rid}")],
    ])


def queued_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([cancel_row(rid)])
