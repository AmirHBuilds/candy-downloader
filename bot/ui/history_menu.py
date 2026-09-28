"""
/history: each user's own download history - what they downloaded, whether
it worked, what quality/format, and when. Paginated, with a way to wipe it.
"""
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.text import esc, site_label

PAGE_SIZE = 8

_STATUS_ICON = {"success": "✓", "failed": "✕", "cancelled": "•"}


def _quality_label(mode: str, quality: str) -> str:
    if mode == "audio":
        return (quality or "audio").upper() if quality else "Audio"
    return (quality or "best").upper()


def _when(iso_at: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_at)
        return dt.strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        return "unknown date"


def history_text(rows: list[dict], page: int, total_pages: int, total: int) -> str:
    if total == 0:
        return "📜 <b>Your history</b>\n\nNothing downloaded yet."

    lines = [f"📜 <b>Your history</b>  ·  page {page}/{total_pages}  ·  {total} total", ""]
    for row in rows:
        icon = _STATUS_ICON.get(row["status"], "•")
        kind = "Audio" if row["mode"] == "audio" else "Video" if row["mode"] == "video" else "File"
        quality = _quality_label(row["mode"], row["quality"])
        site = esc(site_label(row["url"]))
        lines.append(f"{icon} {kind} · {quality} — <i>{site}</i>")
        lines.append(f"   {_when(row['at'])}")
    return "\n".join(lines)


def history_menu(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"hist|page|{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"hist|page|{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("🗑 Clear history", callback_data=f"hist|clear|{page}")])
    rows.append([InlineKeyboardButton("← Back", callback_data="hist|close")])
    return InlineKeyboardMarkup(rows)


def confirm_clear_menu(page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, delete it all", callback_data="hist|clear_yes"),
         InlineKeyboardButton("← No, go back", callback_data=f"hist|page|{page}")],
    ])
