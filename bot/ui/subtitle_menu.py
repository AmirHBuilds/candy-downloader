"""
The subtitles screen (opened from "More options"): pick up to four languages and
how to deliver them. Languages are paged, Persian and English first.

Callback data "dl|sub|<action>|...|<rid>" (rid last, like every dl| button):
  dl|sub|open|<page>|<rid>          show the screen at a page
  dl|sub|t|<index>|<page>|<rid>     toggle one language (index into the track list)
  dl|sub|m|<mode>|<page>|<rid>      embed | file | both
  dl|sub|clr|<rid>                  clear the choice
"Back" is dl|moreq, which returns to More options (where the entry button lives).
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.subtitles import MAX_LANGS, MODE_NAMES, TRACKS_PER_PAGE, SubChoice, clamp_page, page_count
from utils.text import esc

_MODE_LABELS = {"embed": "Embedded", "file": ".srt file", "both": "Both"}
_ORDER = ("embed", "file", "both")


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def subtitles_text(choice: SubChoice, tracks: list, page: int, title: str = "", notice: str = "") -> str:
    pages = page_count(tracks)
    lines = ["◧ <b>Subtitles</b>"]
    if title:
        shown = " ".join(title.split())
        lines.append(f"<i>{esc(shown[:60] + ('…' if len(shown) > 60 else ''))}</i>")
    names = {t.code: t.name for t in tracks}
    chosen = ", ".join(names.get(code, code) for code in choice.langs) or "none"
    lines.append(f"Selected: {esc(chosen)} · {MODE_NAMES[choice.mode]}")
    if pages > 1:
        lines.append(f"Page {clamp_page(tracks, page) + 1} of {pages}")
    lines.append(f"<i>Up to {MAX_LANGS} languages. Telegram's own player may not show an embedded track; "
                 f"the .srt file works in any player.</i>")
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def subtitles_menu(choice: SubChoice, tracks: list, page: int, rid: str) -> InlineKeyboardMarkup:
    page = clamp_page(tracks, page)
    rows = [[_btn(("● " if choice.mode == mode else "○ ") + _MODE_LABELS[mode], f"dl|sub|m|{mode}|{page}|{rid}")
             for mode in _ORDER]]

    first = page * TRACKS_PER_PAGE
    for index in range(first, min(first + TRACKS_PER_PAGE, len(tracks))):
        track = tracks[index]
        mark = "●" if track.code in choice.langs else "○"
        label = f"{mark} {track.name}" + (" · auto" if track.auto else "")
        rows.append([_btn(label, f"dl|sub|t|{index}|{page}|{rid}")])

    pages = page_count(tracks)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("◀ Prev", f"dl|sub|open|{page - 1}|{rid}"))
        if page < pages - 1:
            nav.append(_btn("Next ▶", f"dl|sub|open|{page + 1}|{rid}"))
        rows.append(nav)

    if choice.langs:
        rows.append([_btn("Clear", f"dl|sub|clr|{rid}")])
    rows.append([_btn("← Back", f"dl|moreq|{rid}")])
    return InlineKeyboardMarkup(rows)
