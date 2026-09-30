"""
Screens for the time-range "sections" editor (see downloader/sections.py).

Two screens, both shown by editing the quality-picker message in place:
  editor - saved sections, the next section's Start/End, merge + format choice
  prompt - "send the start time", shown while we wait for a typed timestamp

Telegram inline buttons can't take typed input, so Start/End switch to the
prompt screen and the next text message the person sends becomes the value
(see handle_section_text_input in main.py).

Callback data is "dl|sec|<sub>[|<arg>]|<rid>" - the rid is always last, like
every other dl| button, and the longest one here is well under 64 bytes.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.sections import SectionDraft, format_section, format_timestamp, section_length
from ui.quick_menu import cancel_row
from utils.text import esc

_FMT_LABELS = {"video": "Video", "mp3": "MP3", "opus": "Opus"}


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _value(seconds: float | None) -> str:
    return format_timestamp(seconds) if seconds is not None else "—"


def editor_text(draft: SectionDraft, duration: int, title: str = "", notice: str = "") -> str:
    lines = ["✂ <b>Clip sections</b>"]
    if title:
        shown = " ".join(title.split())
        lines.append(f"<i>{esc(shown[:60] + ('…' if len(shown) > 60 else ''))}</i>")
    lines.append(f"Length: {format_timestamp(duration)}")
    if notice:
        # SectionError text can echo what the person typed - escape it.
        lines += ["", f"⚠ {esc(notice)}"]

    lines.append("")
    if draft.sections:
        lines.append("<b>Saved</b>")
        for i, (start, end) in enumerate(draft.sections, 1):
            lines.append(f"{i}. {format_section(start, end)} · {format_timestamp(section_length(start, end))}")
    else:
        lines.append("No sections saved yet.")

    lines += [
        "",
        "<b>New section</b>",
        f"Start: {_value(draft.start)}   End: {_value(draft.end)}",
        "<i>Empty Start = from the beginning. Empty End = to the end.</i>",
    ]
    return "\n".join(lines)


def editor_menu(draft: SectionDraft, rid: str, has_video: bool) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"Start: {_value(draft.start)}", f"dl|sec|start|{rid}"),
         _btn(f"End: {_value(draft.end)}", f"dl|sec|end|{rid}")],
        [_btn("✓ Save section", f"dl|sec|save|{rid}")],
    ]

    count = len(draft.sections)
    if count:
        # One remove button per saved section, numbered like the list above.
        for i in range(0, count, 5):
            rows.append([_btn(f"✕ {n}", f"dl|sec|del|{n}|{rid}") for n in range(i + 1, min(i + 5, count) + 1)])

    if count >= 2:
        rows.append([
            _btn(("● " if not draft.merge else "○ ") + "Separate clips", f"dl|sec|merge|off|{rid}"),
            _btn(("● " if draft.merge else "○ ") + "Merged into one", f"dl|sec|merge|on|{rid}"),
        ])

    formats = (["video"] if has_video else []) + ["mp3", "opus"]
    rows.append([_btn(("● " if draft.fmt == f else "○ ") + _FMT_LABELS[f], f"dl|sec|fmt|{f}|{rid}") for f in formats])

    rows.append([_btn(f"↓ Download {count} clip{'s' if count != 1 else ''}" if count else "↓ Download",
                      f"dl|sec|go|{rid}")])
    rows.append([_btn("← Back", f"dl|sec|back|{rid}")])
    rows.append(cancel_row(rid))
    return InlineKeyboardMarkup(rows)


def prompt_text(field: str, duration: int, notice: str = "") -> str:
    lines = [f"Send the <b>{'start' if field == 'start' else 'end'}</b> time.", ""]
    if notice:
        lines += [f"⚠ {esc(notice)}", ""]
    lines += [
        "Examples: <code>1:50:00</code>  ·  <code>50:00</code>  ·  <code>90</code> (seconds)",
        f"Length: {format_timestamp(duration)}",
    ]
    return "\n".join(lines)


def prompt_menu(field: str, rid: str) -> InlineKeyboardMarkup:
    empty_label = "Leave empty (from the start)" if field == "start" else "Leave empty (to the end)"
    return InlineKeyboardMarkup([
        [_btn(empty_label, f"dl|sec|clear|{field}|{rid}")],
        [_btn("← Back", f"dl|sec|edit|{rid}")],
    ])

