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
    adhd_on = s.get("adhd_mode", False)
    rows = [
        _row(InlineKeyboardButton(
            f"{'🧠⚡ ADHD Mode: ON' if adhd_on else '🧠 ADHD Mode: off'}",
            callback_data="s|adhd_mode|" + ("0" if adhd_on else "1"))),
        _row(InlineKeyboardButton("⚙️ Advanced (speed / proxy)", callback_data="nav|advanced")),
        _row(InlineKeyboardButton("🔑 Cookies help", callback_data="nav|cookies")),
        _row(InlineKeyboardButton("↻ Reset to defaults", callback_data="nav|reset")),
        _row(InlineKeyboardButton("← Back to start", callback_data="nav|home")),
    ]
    return InlineKeyboardMarkup(rows)


SETTINGS_LEGEND = (
    "<b>ADHD Mode</b> — send a link, get the file. No quality picker, no "
    "questions: always grabs the best video quality it can. Toggle any "
    "time with /adhd_on or /adhd_off.\n\n"
    "Everything else (quality, format, playlist range...) is just asked "
    "when you send a link."
)


def back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="nav|main")]])


def advanced_menu(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚦 Rate limit: {s['rate_limit_kbps'] or 'unlimited'} KB/s (tap to set)",
                               callback_data="nav|rate_limit")],
        [InlineKeyboardButton(f"🧵 Parallel fragments: {s['concurrent_fragments']} (tap to set)",
                               callback_data="nav|fragments")],
        [InlineKeyboardButton(f"🌐 Proxy: {s['proxy'] or 'none'} (tap to set)",
                               callback_data="nav|proxy")],
        [InlineKeyboardButton(
            f"🍪 Cookies: {'enabled' if s['cookies_enabled'] else 'disabled'}",
            callback_data="s|cookies_enabled|" + ("0" if s["cookies_enabled"] else "1"))],
        [InlineKeyboardButton("← Back", callback_data="nav|main")],
    ])


def confirm_reset_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, reset everything", callback_data="s|__reset__|1"),
         InlineKeyboardButton("← No, go back", callback_data="nav|main")],
    ])


# These three "advanced" rows show a value but had no screen behind them -
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
}
