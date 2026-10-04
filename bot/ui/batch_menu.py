"""
Screens for downloading several things at once: a playlist, or a message with
several links. Pure rendering - the state lives in jobqueue/batch.py (used
here by duck typing, so this module imports nothing from it).

  picker   - paged list of items, tap to select, Select all / Clear
  quality  - one quality for the whole selection
  running  - (menu only) Cancel all, under the shared progress message
  summary  - (menu only) Retry failed / Dismiss, once it has finished

Callback data is "bt|<action>|...|<bid>", the batch id always last:
  bt|t|<index>|<page>|<bid>   toggle one item        bt|p|<page>|<bid>   change page
  bt|sa|<page>|<bid>          select all             bt|cl|<page>|<bid>  clear
  bt|q|<bid>                  pick quality           bt|qa|<bid>         select all, then pick quality
  bt|go|<quality>|<bid>       start                  bt|bk|<bid>         back to the list
  bt|x|<bid>                  close the picker       bt|cx|<bid>         cancel the running batch
  bt|rt|<bid>                 retry failed           bt|dm|<bid>         dismiss the summary
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.sections import format_timestamp
from utils.text import esc

ITEMS_PER_PAGE = 8
MAX_BATCH_DOWNLOAD = 50          # most items one batch will download (keeps the queue and Telegram's rate limits sane)
QUALITIES = ("best", "1080p", "720p", "480p", "worst", "mp3", "opus")
TITLE_CHARS = 26


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def page_count(batch) -> int:
    return max(1, -(-len(batch.items) // ITEMS_PER_PAGE))


def clamp_page(batch, page: int) -> int:
    return max(0, min(page, page_count(batch) - 1))


def _short(title: str, limit: int = TITLE_CHARS) -> str:
    title = " ".join((title or "").split())
    return title[:limit].rstrip() + "…" if len(title) > limit else title


def picker_text(batch, page: int, notice: str = "") -> str:
    pages = page_count(batch)
    lines = [f"📋 <b>{esc(_short(batch.title or 'Your links', 50))}</b>"]
    summary = f"{len(batch.items)} item{'s' if len(batch.items) != 1 else ''} · {len(batch.selected)} selected"
    if pages > 1:
        summary += f" · page {clamp_page(batch, page) + 1} of {pages}"
    lines.append(summary)
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def picker_menu(batch, page: int) -> InlineKeyboardMarkup:
    page = clamp_page(batch, page)
    pages = page_count(batch)
    bid = batch.bid
    rows = []
    first = page * ITEMS_PER_PAGE
    for index in range(first, min(first + ITEMS_PER_PAGE, len(batch.items))):
        item = batch.items[index]
        mark = "●" if index in batch.selected else "○"
        length = f" · {format_timestamp(item.duration)}" if item.duration else ""
        rows.append([_btn(f"{mark} {index + 1}. {_short(item.title)}{length}", f"bt|t|{index}|{page}|{bid}")])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("◀ Prev", f"bt|p|{page - 1}|{bid}"))
        if page < pages - 1:
            nav.append(_btn("Next ▶", f"bt|p|{page + 1}|{bid}"))
        rows.append(nav)

    rows.append([_btn("Select all", f"bt|sa|{page}|{bid}"), _btn("Clear", f"bt|cl|{page}|{bid}")])
    rows.append([_btn(f"↓ Download selected ({len(batch.selected)})", f"bt|q|{bid}")])
    rows.append([_btn(f"↓ Download all ({min(len(batch.items), MAX_BATCH_DOWNLOAD)})", f"bt|qa|{bid}")])
    rows.append([_btn("✕ Close", f"bt|x|{bid}")])
    return InlineKeyboardMarkup(rows)


def quality_text(batch) -> str:
    n = len(batch.selected)
    return f"📋 <b>Download {n} item{'s' if n != 1 else ''}</b>\nPick a quality for all of them."


def quality_menu(batch) -> InlineKeyboardMarkup:
    bid = batch.bid
    return InlineKeyboardMarkup([
        [_btn("★ Best available", f"bt|go|best|{bid}")],
        [_btn("1080p", f"bt|go|1080p|{bid}"), _btn("720p", f"bt|go|720p|{bid}"), _btn("480p", f"bt|go|480p|{bid}")],
        [_btn("↓ Smallest size", f"bt|go|worst|{bid}")],
        [_btn("♪ MP3", f"bt|go|mp3|{bid}"), _btn("♪ Opus", f"bt|go|opus|{bid}")],
        [_btn("← Back", f"bt|bk|{bid}")],
    ])


def running_menu(batch) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("✕ Cancel all", f"bt|cx|{batch.bid}")]])


def summary_menu(batch, failed: int) -> InlineKeyboardMarkup:
    rows = []
    if failed:
        rows.append([_btn(f"↻ Retry failed ({failed})", f"bt|rt|{batch.bid}")])
    rows.append([_btn("Dismiss", f"bt|dm|{batch.bid}")])
    return InlineKeyboardMarkup(rows)
