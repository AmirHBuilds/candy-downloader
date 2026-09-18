"""
Menus shown right after a link is detected. Quality buttons are built
from what the link *actually* offers (see downloader/probe.py) rather
than a fixed guess - a 720p-max video only shows 720p and below, and
sites with no real quality concept (galleries, direct files) skip
quality selection entirely.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.probe import ProbeResult


def cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("✕ Cancel", callback_data="dl|cancel")]


def video_menu(probe: ProbeResult) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("★ Best available", callback_data="dl|video|best")]]

    # Only offer specific resolutions that actually exist for this link,
    # capped to a handful so the menu stays short.
    shown = [h for h in probe.heights if h]
    picks = []
    for h in shown:
        if not picks or (picks[-1] - h) >= 120:  # skip near-duplicate resolutions
            picks.append(h)
        if len(picks) == 3:
            break
    if picks:
        rows.append([
            InlineKeyboardButton(f"{h}p", callback_data=f"dl|video|{h}p") for h in picks
        ])

    if probe.has_audio:
        rows.append([
            InlineKeyboardButton("♪ MP3", callback_data="dl|audio|mp3"),
            InlineKeyboardButton("♪ Opus", callback_data="dl|audio|opus"),
        ])

    # room to grow: more resolutions, formats, etc. live behind this
    rows.append([InlineKeyboardButton("More options…", callback_data="dl|moreq")])
    rows.append(cancel_row())
    return InlineKeyboardMarkup(rows)


def extended_video_menu(probe: ProbeResult) -> InlineKeyboardMarkup:
    """The fuller quality list, one level deeper than the default menu -
    reachable via "More options" and returns to it via "Back"."""
    rows = []
    picks = []
    for h in probe.heights or []:
        if not picks or (picks[-1] - h) >= 60:
            picks.append(h)
    for i in range(0, len(picks), 3):
        rows.append([
            InlineKeyboardButton(f"{h}p", callback_data=f"dl|video|{h}p") for h in picks[i:i + 3]
        ])
    rows.append([InlineKeyboardButton("↓ Smallest size", callback_data="dl|video|worst")])
    rows.append([InlineKeyboardButton("← Back", callback_data="dl|backq")])
    rows.append(cancel_row())
    return InlineKeyboardMarkup(rows)


def simple_menu() -> InlineKeyboardMarkup:
    """For galleries/direct files - no quality concept, just go/cancel."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↓ Download", callback_data="dl|simple|download")],
        cancel_row(),
    ])


def fallback_menu() -> InlineKeyboardMarkup:
    """Used only if probing the link failed - three plain choices, no
    fake specific-resolution options we can't actually confirm exist."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("★ Best quality", callback_data="dl|video|best")],
        [InlineKeyboardButton("↓ Smallest size", callback_data="dl|video|worst"),
         InlineKeyboardButton("♪ Audio only", callback_data="dl|audio|mp3")],
        cancel_row(),
    ])


def spotify_menu() -> InlineKeyboardMarkup:
    """Spotify has no video/quality concept at all - just one button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♪ Download MP3", callback_data="dl|audio|mp3")],
        cancel_row(),
    ])


def send_as_file_menu() -> InlineKeyboardMarkup:
    """Offered after a video download completes, in case Telegram's video
    compression/preview isn't what the person wants - re-fetches and sends
    the same content as a plain file instead."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▤ Send as file instead", callback_data="dl|asfile|x")],
    ])


def retry_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↻ Try again", callback_data="dl|retry")]])


def queued_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([cancel_row()])
