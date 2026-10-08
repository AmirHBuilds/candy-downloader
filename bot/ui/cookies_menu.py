"""The cookies screen: how-to text, which sites you have cookies for, and a remove button for each."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.cookies import SiteInfo, site_label
from utils.text import esc


def cookies_list_text(sites: list[SiteInfo]) -> str:
    if not sites:
        return "<b>Your cookies</b>\nNone yet — send a cookies.txt file."
    lines = ["<b>Your cookies</b>"]
    for info in sites:
        note = " — ⚠ expired, export again" if info.expired else ""
        lines.append(f"• {esc(site_label(info.site))} · {info.count} cookies{note}")
    lines.append("<i>Sending a file for a site replaces only that site's cookies.</i>")
    return "\n".join(lines)


def cookies_menu(sites: list[SiteInfo], back: str = "nav|main") -> InlineKeyboardMarkup:
    # callback data: ck|rm|<site>  (a site is a domain: far below Telegram's 64-byte limit)
    rows = [[InlineKeyboardButton(f"🗑 Remove {site_label(info.site)}", callback_data=f"ck|rm|{info.site}"[:64])]
            for info in sites]
    rows.append([InlineKeyboardButton("← Back", callback_data=back)])
    return InlineKeyboardMarkup(rows)
