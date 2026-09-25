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
    # "Thumbnail" meant different things depending on mode and wasn't
    # obvious either way: for audio it embeds cover art players show; for
    # video it embeds a thumbnail frame that's rarely visible since
    # Telegram already generates its own preview for video messages.
    thumb_label = "Cover art" if s["mode"] == "audio" else "Embed thumbnail"
    rows = [
        _row(InlineKeyboardButton(f"Mode: {mode_label}", callback_data="nav|mode")),
        _row(InlineKeyboardButton(f"Quality: {s['quality']}", callback_data="nav|quality")),
        _row(InlineKeyboardButton(f"Subtitles: {s['subtitles']}", callback_data="nav|subs")),
        _row(InlineKeyboardButton(f"Playlist: {s['playlist_mode']}", callback_data="nav|playlist")),
        _row(InlineKeyboardButton(
            f"{thumb_label}: {'on' if s['embed_thumbnail'] else 'off'}",
            callback_data="s|embed_thumbnail|" + ("0" if s["embed_thumbnail"] else "1")),
            InlineKeyboardButton(
            f"Tag title/artist: {'on' if s['embed_metadata'] else 'off'}",
            callback_data="s|embed_metadata|" + ("0" if s["embed_metadata"] else "1")),
        ),
        _row(InlineKeyboardButton(
            f"SponsorBlock: {'on' if s['sponsorblock'] else 'off'}",
            callback_data="s|sponsorblock|" + ("0" if s["sponsorblock"] else "1")),
            InlineKeyboardButton(
            f"Skip already sent: {'on' if s['use_archive'] else 'off'}",
            callback_data="s|use_archive|" + ("0" if s["use_archive"] else "1")),
        ),
        _row(InlineKeyboardButton("⚙ Advanced (network/proxy/cookies)", callback_data="nav|advanced")),
        _row(InlineKeyboardButton("Cookies help", callback_data="nav|cookies")),
        _row(InlineKeyboardButton("↻ Reset to defaults", callback_data="nav|reset")),
        _row(InlineKeyboardButton("← Back to start", callback_data="nav|home")),
    ]
    return InlineKeyboardMarkup(rows)


SETTINGS_LEGEND = (
    "<i>Tag title/artist</i> writes the video/track's title and uploader "
    "into the file's metadata. <i>SponsorBlock</i> auto-skips sponsor "
    "segments (YouTube only). <i>Skip already sent</i> won't re-download "
    "a link you've already gotten before."
)


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
            (f"Range: {s['playlist_range']} (tap to change)" if s["playlist_range"] else "Set range (tap here)")
            if s["playlist_mode"] == "range" else "Range: n/a (only used in \"range\" mode)",
            callback_data="nav|playlist_range")],
        [InlineKeyboardButton("← Back", callback_data="nav|main")],
    ])


def advanced_menu(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Rate limit: {s['rate_limit_kbps'] or 'unlimited'} KB/s (tap to set)",
                               callback_data="nav|rate_limit")],
        [InlineKeyboardButton(f"Parallel fragments: {s['concurrent_fragments']} (tap to set)",
                               callback_data="nav|fragments")],
        [InlineKeyboardButton(f"Proxy: {s['proxy'] or 'none'} (tap to set)",
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


# These four "advanced" rows show a value but had no screen behind them -
# tapping did nothing. Each entry here gives main.py everything it needs
# to prompt for, parse, and apply a typed reply: the setting key it
# writes, what to ask for, and a parser that returns (value, confirmation)
# or raises ValueError with a message to show back to the user.
def _parse_rate_limit(text: str):
    low = text.strip().lower()
    if low in ("off", "none", "unlimited", "0", "-"):
        return 0, "Rate limit removed — unlimited."
    if text.strip().isdigit() and int(text.strip()) > 0:
        return int(text.strip()), f"Rate limit set to {int(text.strip())} KB/s."
    raise ValueError("That's not a valid number. Send e.g. 500, or \"off\" for unlimited.")


def _parse_fragments(text: str):
    cleaned = text.strip()
    if cleaned.isdigit() and 1 <= int(cleaned) <= 16:
        return int(cleaned), f"Parallel fragments set to {int(cleaned)}."
    raise ValueError("Send a whole number from 1 to 16.")


def _parse_proxy(text: str):
    low = text.strip().lower()
    if low in ("off", "none", "-", "clear"):
        return "", "Proxy cleared."
    if "://" in text.strip():
        return text.strip(), "Proxy set."
    raise ValueError("Send a full proxy URL like socks5://host:port, or \"off\" to clear it.")


def _parse_playlist_range(text: str):
    import re
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", text)
    if m and int(m.group(1)) >= 1 and int(m.group(2)) >= int(m.group(1)):
        value = f"{int(m.group(1))}-{int(m.group(2))}"
        return value, f"Playlist range set to {value}."
    raise ValueError("Send a range like 3-8 (start must be ≤ end).")


ADVANCED_TEXT_FIELDS = {
    "rate_limit": {
        "setting_key": "rate_limit_kbps",
        "prompt": "Send a download speed limit in KB/s (e.g. 500), or \"off\" for unlimited.",
        "parser": _parse_rate_limit,
    },
    "fragments": {
        "setting_key": "concurrent_fragments",
        "prompt": "Send a number from 1 to 16 for how many pieces to download in parallel.",
        "parser": _parse_fragments,
    },
    "proxy": {
        "setting_key": "proxy",
        "prompt": "Send a proxy URL, e.g. socks5://host:port or http://host:port, or \"off\" to clear it.",
        "parser": _parse_proxy,
    },
    "playlist_range": {
        "setting_key": "playlist_range",
        "prompt": "Send a range like 3-8 — downloads items 3 through 8 of the playlist.",
        "parser": _parse_playlist_range,
    },
}
