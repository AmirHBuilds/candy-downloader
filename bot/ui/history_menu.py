"""
/history: each user's own download history - what they downloaded, whether
it worked, what quality/format, and when. Paginated, with a way to wipe it.
"""
import html
from datetime import datetime
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils.text import esc, site_label

PAGE_SIZE = 8
TITLE_MAX_CHARS = 28   # visible characters of the title before the "…"

# Where the history screen was opened from, so "← Back" can return there:
# "h" = the /start welcome screen (also the default for the /history command),
# "s" = the /settings screen. Kept to one character - callback_data is 64 bytes.
ORIGIN_HOME = "h"
ORIGIN_SETTINGS = "s"

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


def _short_title(title: str) -> str:
    """A few characters of the title, then "…". Collapses whitespace first
    (titles can contain newlines). Slices by code point, so a ZWJ emoji
    sequence could in theory be cut mid-sequence - harmless."""
    title = " ".join((title or "").split())
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip() + "…"
    return title


def _linked(text: str, url: str) -> str:
    """<a href> around already-escaped text. The URL goes into an ATTRIBUTE,
    so unlike esc() (which leaves quotes alone - fine for text) it must be
    escaped with quote=True, or a stray " in the URL would break the tag.
    Only http(s) URLs are linked; anything else stays plain text."""
    if urlparse(url).scheme not in ("http", "https"):
        return text
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def history_text(rows: list[dict], page: int, total_pages: int, total: int) -> str:
    if total == 0:
        return "📜 <b>Your history</b>\n\nNothing downloaded yet."

    lines = [f"📜 <b>Your history</b>  ·  page {page}/{total_pages}  ·  {total} total", ""]
    for row in rows:
        icon = _STATUS_ICON.get(row["status"], "•")
        kind = "Audio" if row["mode"] == "audio" else "Video" if row["mode"] == "video" else "File"
        quality = _quality_label(row["mode"], row["quality"])
        site = esc(site_label(row["url"]))
        title = _short_title(row.get("title", ""))
        # Title-as-link when we know it; otherwise the site name is the link
        # (old rows, and failed/cancelled jobs, have no title).
        label = _linked(esc(title), row["url"]) if title else _linked(f"<i>{site}</i>", row["url"])
        lines.append(f"{icon} {kind} · {quality} — {label}")
        meta = f"{site} · " if title else ""
        lines.append(f"   {meta}{_when(row['at'])}")
    return "\n".join(lines)


def history_menu(page: int, total_pages: int, origin: str = ORIGIN_HOME) -> InlineKeyboardMarkup:
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"hist|page|{page - 1}|{origin}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"hist|page|{page + 1}|{origin}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("🗑 Clear history", callback_data=f"hist|clear|{page}|{origin}")])
    rows.append([InlineKeyboardButton("← Back", callback_data=f"hist|close|{origin}")])
    return InlineKeyboardMarkup(rows)


def confirm_clear_menu(page: int, origin: str = ORIGIN_HOME) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Yes, delete it all", callback_data=f"hist|clear_yes|{origin}"),
         InlineKeyboardButton("← No, go back", callback_data=f"hist|page|{page}|{origin}")],
    ])
