"""
Builds the /settings inline-keyboard UI. Everything is buttons - no typing
commands - and callback_data encodes exactly what to change.

callback_data format: "s|<key>|<value>"  or  "nav|<screen>"
Kept short because Telegram limits callback_data to 64 bytes.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from settings.user_settings import DEFAULTS
from ui.progress import BAR_STYLES


def _row(*buttons):
    return list(buttons)


def main_menu(s: dict) -> InlineKeyboardMarkup:
    adhd_on = s.get("adhd_mode", False)
    sizes_on = s.get("show_sizes", True)
    rows = [
        _row(InlineKeyboardButton(
            f"{'🧠⚡ ADHD Mode: ON' if adhd_on else '🧠 ADHD Mode: off'}",
            callback_data="s|adhd_mode|" + ("0" if adhd_on else "1"))),
        _row(InlineKeyboardButton(
            f"📏 Sizes on buttons: {'ON' if sizes_on else 'off'}",
            callback_data="s|show_sizes|" + ("0" if sizes_on else "1"))),
        _row(InlineKeyboardButton("🎨 Progress bar style", callback_data="nav|bars")),
        _row(InlineKeyboardButton("📜 History", callback_data="misc|history")),
        _row(InlineKeyboardButton("⚙️ Advanced (speed / proxy)", callback_data="nav|advanced")),
        _row(InlineKeyboardButton("🔑 Cookies help", callback_data="nav|cookies")),
        _row(InlineKeyboardButton("↻ Reset to defaults", callback_data="nav|reset")),
        _row(InlineKeyboardButton("← Back to start", callback_data="nav|home")),
    ]
    return InlineKeyboardMarkup(rows)


def bars_title() -> str:
    samples = [f"{label}\n{fn(60)}" for label, fn in BAR_STYLES.values()]
    return ("<b>Progress bar style</b>\n\n" + "\n\n".join(samples)
            + "\n\nAuto = moons in ADHD Mode, candy otherwise.")


def bars_menu(s: dict) -> InlineKeyboardMarkup:
    current = s.get("bar_style", "auto")
    rows = [[InlineKeyboardButton(("✓ " if current == "auto" else "") + "Auto", callback_data="s|bar_style|auto")]]
    names = list(BAR_STYLES.items())
    for i in range(0, len(names), 2):
        rows.append([
            InlineKeyboardButton(("✓ " if current == key else "") + label, callback_data=f"s|bar_style|{key}")
            for key, (label, _fn) in names[i:i + 2]
        ])
    rows.append([InlineKeyboardButton("← Back", callback_data="nav|main")])
    return InlineKeyboardMarkup(rows)


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
