"""
The subtitles screen (opened from "More options"): pick up to four languages and
how to deliver them. Languages are paged, Persian and English first.

Callback data "dl|sub|<action>|...|<rid>" (rid last, like every dl| button):
  dl|sub|open|<page>|<rid>          show the screen at a page
  dl|sub|t|<index>|<page>|<rid>     toggle one language (index into the track list)
  dl|sub|m|<option>|<page>|<rid>    switch one of: embed | file | burn (embed and burn exclude each other)
  dl|sub|clr|<rid>                  clear the choice
"Back" is dl|moreq, which returns to More options (where the entry button lives).
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.subtitles import (
    MAX_LANGS, MODE_NAMES, OPTION_LABELS, OPTIONS, TRACKS_PER_PAGE, SubChoice, burn_split, clamp_page, page_count,
)
from utils.text import esc



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
    burned, others = burn_split(choice, tracks)
    if burned:
        lines.append(f"🔥 Burning in: <b>{esc(burned)}</b> (the first one you tapped)")
        if choice.has("file"):
            lines.append("All selected languages also come as .srt files.")
        elif others:
            lines.append(f"Sent as .srt files: {esc(', '.join(others))}")
    if pages > 1:
        lines.append(f"Page {clamp_page(tracks, page) + 1} of {pages}")
    lines.append(f"<i>Up to {MAX_LANGS} languages. Telegram's own player may not show an embedded track; "
                 f"the .srt file works in any player.</i>")
    if choice.has("burn"):
        lines.append("<i>Burned in is drawn into the picture, so every player shows it. It uses the first language "
                     "(the others come as .srt files unless you switch .srt file on) and re-encodes the video, which takes longer.</i>")
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def subtitles_menu(choice: SubChoice, tracks: list, page: int, rid: str) -> InlineKeyboardMarkup:
    page = clamp_page(tracks, page)
    # Three independent switches (Embedded and Burned in switch each other off).
    rows = [[_btn(("● " if choice.has(option) else "○ ") + OPTION_LABELS[option], f"dl|sub|m|{option}|{page}|{rid}")
             for option in OPTIONS]]

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
