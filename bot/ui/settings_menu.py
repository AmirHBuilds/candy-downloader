"""
Builds the /settings inline-keyboard UI. Everything is buttons - no typing
commands - and callback_data encodes exactly what to change.

callback_data format: "s|<key>|<value>"  or  "nav|<screen>"
Kept short because Telegram limits callback_data to 64 bytes.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings.user_settings import DEFAULTS


def _row(*buttons):
    return list(buttons)


def main_menu(s: dict) -> InlineKeyboardMarkup:
    mode_label = "Video" if s["mode"] == "video" else "Audio only"
    rows = [
        _row(InlineKeyboardButton(f"Mode: {mode_label}", callback_data="nav|mode")),
        _row(InlineKeyboardButton(f"Quality: {s['quality']}", callback_data="nav|quality")),
        _row(InlineKeyboardButton(f"Subtitles: {s['subtitles']}", callback_data="nav|subs")),
        _row(InlineKeyboardButton(f"Playlist: {s['playlist_mode']}", callback_data="nav|playlist")),
        _row(InlineKeyboardButton(
            f"Thumbnail: {'on' if s['embed_thumbnail'] else 'off'}",
            callback_data="s|embed_thumbnail|" + ("0" if s["embed_thumbnail"] else "1")),
            InlineKeyboardButton(
            f"Metadata: {'on' if s['embed_metadata'] else 'off'}",
            callback_data="s|embed_metadata|" + ("0" if s["embed_metadata"] else "1")),
        ),
        _row(InlineKeyboardButton(
            f"SponsorBlock: {'on' if s['sponsorblock'] else 'off'}",
            callback_data="s|sponsorblock|" + ("0" if s["sponsorblock"] else "1")),
            InlineKeyboardButton(
            f"Skip repeats: {'on' if s['use_archive'] else 'off'}",
            callback_data="s|use_archive|" + ("0" if s["use_archive"] else "1")),
        ),
        _row(InlineKeyboardButton("⚙ Advanced (network/proxy/cookies)", callback_data="nav|advanced")),
        _row(InlineKeyboardButton("Cookies help", callback_data="nav|cookies")),
        _row(InlineKeyboardButton("↻ Reset to defaults", callback_data="nav|reset")),
        _row(InlineKeyboardButton("← Back to start", callback_data="nav|home")),
    ]
    return InlineKeyboardMarkup(rows)


def back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="nav|main")]])


def mode_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        _row(InlineKeyboardButton("Video", callback_data="s|mode|video"),
             InlineKeyboardButton("Audio only", callback_data="s|mode|audio")),
        _row(InlineKeyboardButton("← Back", callback_data="nav|main")),
    ])


def quality_menu(s: dict) -> InlineKeyboardMarkup:
    options = ["best", "1080p", "720p", "480p", "360p", "worst"]
    rows = []
    for i in range(0, len(options), 3):
        rows.append([
            InlineKeyboardButton(("✓ " if s["quality"] == o else "") + o, callback_data=f"s|quality|{o}")
            for o in options[i:i + 3]
        ])
    if s["mode"] == "audio":
        rows.append([
            InlineKeyboardButton(("✓ " if s["audio_format"] == f else "") + f, callback_data=f"s|audio_format|{f}")
            for f in ["mp3", "m4a", "opus", "flac"]
        ])
    rows.append([InlineKeyboardButton("← Back", callback_data="nav|main")])
    return InlineKeyboardMarkup(rows)


def subs_menu(s: dict) -> InlineKeyboardMarkup:
    options = ["off", "auto", "manual"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✓ " if s["subtitles"] == o else "") + o, callback_data=f"s|subtitles|{o}")
         for o in options],
        [InlineKeyboardButton(
            f"Embed in video: {'on' if s['embed_subtitles'] else 'off'}",
            callback_data="s|embed_subtitles|" + ("0" if s["embed_subtitles"] else "1"))],
        [InlineKeyboardButton("← Back", callback_data="nav|main")],
    ])


def playlist_menu(s: dict) -> InlineKeyboardMarkup:
    options = ["single", "full", "range"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✓ " if s["playlist_mode"] == o else "") + o, callback_data=f"s|playlist_mode|{o}")
         for o in options],
        [InlineKeyboardButton(
            "Set range (reply to bot with e.g. 3-8)" if s["playlist_mode"] == "range" else "Range: n/a",
            callback_data="nav|playlist_range")],
        [InlineKeyboardButton("← Back", callback_data="nav|main")],
    ])


def advanced_menu(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Rate limit: {s['rate_limit_kbps'] or 'unlimited'} KB/s",
                               callback_data="nav|rate_limit")],
        [InlineKeyboardButton(f"Parallel fragments: {s['concurrent_fragments']}",
                               callback_data="nav|fragments")],
        [InlineKeyboardButton(f"Proxy: {s['proxy'] or 'none'} (reply to set)",
                               callback_data="nav|proxy")],
        [InlineKeyboardButton(
            f"Cookies: {'enabled' if s['cookies_enabled'] else 'disabled'}",
            callback_data="s|cookies_enabled|" + ("0" if s["cookies_enabled"] else "1"))],
        [InlineKeyboardButton("← Back", callback_data="nav|main")],
    ])


def confirm_reset_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, reset everything", callback_data="s|__reset__|1"),
         InlineKeyboardButton("← No, go back", callback_data="nav|main")],
    ])
