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
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

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


def link_row(url: str) -> list[InlineKeyboardButton]:
    """A tap-to-copy button for the original source link. Telegram deletes
    the person's own message once we've picked up its URL (see
    link_handler), and the status message that shows it as <code> text
    also gets deleted once the file is delivered - without this, the link
    is gone from the chat entirely the moment the download finishes."""
    # Telegram limits a copy-text payload to 256 characters - fall back to
    # a plain (non-copy) link-styled button rather than crashing if some
    # unusually long URL ever exceeds that.
    if len(url) > 256:
        return [InlineKeyboardButton("🔗 Video link", url=url)]
    return [InlineKeyboardButton("🔗 Video link", copy_text=CopyTextButton(text=url))]


def send_as_file_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▤ Send as file instead", callback_data=f"dl|asfile|{rid}")],
        link_row(url),
    ])


def sent_menu(url: str) -> InlineKeyboardMarkup:
    """Attached to audio/document sends, which have no "send as file"
    choice of their own - just keeps the source link copyable."""
    return InlineKeyboardMarkup([link_row(url)])


def redo_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    """Same Try-again + Delete shape as retry_menu, but for cancelling
    before a quality pick was even made - there's no completed download
    settings to retry yet, so this re-shows the quality picker instead."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Try again", callback_data=f"dl|redo|{rid}"),
         InlineKeyboardButton("✕ Delete", callback_data=f"dl|dismiss|{rid}")],
        link_row(url),
    ])


def retry_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    """Used for every terminal non-success state (cancelled, failed,
    expired) so "Try again" and "Delete" are always both offered together
    - previously a couple of code paths built their own ad-hoc
    Try-again-only markup, so the delete button only showed up
    sometimes. Also the only place the source link survives on a
    cancelled/failed download, now that it's no longer inlined into the
    message text."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Try again", callback_data=f"dl|retry|{rid}"),
         InlineKeyboardButton("✕ Delete", callback_data=f"dl|dismiss|{rid}")],
        link_row(url),
    ])


# Same shape, kept as a separate name where the call site is specifically
# about a cancellation rather than a failure - purely for readability.
cancelled_menu = retry_menu


def queued_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([cancel_row(rid)])
