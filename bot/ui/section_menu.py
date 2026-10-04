"""
Screens for the time-range "sections" editor (see downloader/sections.py).

The editor ONLY edits sections. Quality / audio is chosen on the normal quality
menu afterwards, and those buttons then download just the sections chosen here
(the menu's caption lists them, see sections_summary). So there is no Save and
no Download button in here:

    [+ Add section]
    [Start: 1:30:00] [End: 1:32:00] [✕]
    [Start: —      ] [End: —      ] [✕]
    [● Separate clips] [○ Merged into one]      <- only with 2+ sections
    [← Back]

Start/End can't take typed input from a button, so tapping one switches to the
prompt screen and the next text message becomes the value (see
handle_section_text_input in main.py).

Callback data is "dl|sec|<sub>[|<args>]|<rid>" - the rid is always last, like
every other dl| button; the longest one here is well under Telegram's 64 bytes.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.sections import SectionDraft, format_section, format_timestamp
from utils.text import esc

CHAPTERS_PER_PAGE = 8
MAX_SUMMARY_LINES = 4      # keep the quality menu's caption short (a photo caption is capped at 1024 chars)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _value(seconds: float | None) -> str:
    return format_timestamp(seconds) if seconds is not None else "—"


def editor_text(draft: SectionDraft, duration: int, title: str = "", notice: str = "") -> str:
    lines = ["✄ <b>Clip sections</b>"]
    if title:
        shown = " ".join(title.split())
        lines.append(f"<i>{esc(shown[:60] + ('…' if len(shown) > 60 else ''))}</i>")
    lines.append(f"Length: {format_timestamp(duration)}")
    if notice:
        # SectionError text can echo what the person typed - escape it.
        lines += ["", f"⚠ {esc(notice)}"]
    lines += ["", "<i>Go back and pick a quality to download.</i>"]
    return "\n".join(lines)


def editor_menu(draft: SectionDraft, rid: str, active_count: int, has_chapters: bool = False) -> InlineKeyboardMarkup:
    top = [_btn("+ Add section", f"dl|sec|add|{rid}")]
    if has_chapters:
        top.append(_btn("Chapters", f"dl|sec|chap|0|{rid}"))     # one-tap ranges from the video's own chapters
    rows = [top]

    for i, row in enumerate(draft.rows):
        line = [_btn(f"Start: {_value(row.start)}", f"dl|sec|start|{i}|{rid}"),
                _btn(f"End: {_value(row.end)}", f"dl|sec|end|{i}|{rid}")]
        # Removing: a lone empty row has nothing to remove; otherwise every row gets a ✕.
        if len(draft.rows) > 1 or not row.is_empty():
            line.append(_btn("✕", f"dl|sec|del|{i}|{rid}"))
        rows.append(line)

    if active_count >= 2:
        rows.append([
            _btn(("● " if not draft.merge else "○ ") + "Separate clips", f"dl|sec|merge|off|{rid}"),
            _btn(("● " if draft.merge else "○ ") + "Merged into one", f"dl|sec|merge|on|{rid}"),
        ])

    rows.append([_btn("← Back", f"dl|sec|back|{rid}")])
    return InlineKeyboardMarkup(rows)


def chapter_pages(chapters: list) -> int:
    return max(1, -(-len(chapters) // CHAPTERS_PER_PAGE))


def chapters_text(title: str, duration: int, page: int, pages: int, notice: str = "") -> str:
    lines = ["✄ <b>Chapters</b>"]
    if title:
        shown = " ".join(title.split())
        lines.append(f"<i>{esc(shown[:60] + ('…' if len(shown) > 60 else ''))}</i>")
    lines.append("Tap a chapter to add it as a section; tap again to remove it.")
    if pages > 1:
        lines.append(f"Page {page + 1} of {pages}")
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def chapters_menu(chapters: list, draft: SectionDraft, rid: str, page: int) -> InlineKeyboardMarkup:
    """chapters = [(title, start, end)]. A chapter shows ✓ once it is one of the
    sections. Button text is plain text, so titles need no escaping."""
    pages = chapter_pages(chapters)
    page = max(0, min(page, pages - 1))
    chosen = {(row.start, row.end) for row in draft.rows}
    rows = []
    first = page * CHAPTERS_PER_PAGE
    for offset, (name, start, end) in enumerate(chapters[first:first + CHAPTERS_PER_PAGE]):
        mark = "✓ " if (float(start), float(end)) in chosen else ""
        shown = " ".join(name.split())
        label = f"{mark}{format_timestamp(start)} · {shown[:28] + ('…' if len(shown) > 28 else '')}"
        rows.append([_btn(label, f"dl|sec|ch|{first + offset}|{page}|{rid}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_btn("◀ Prev", f"dl|sec|chap|{page - 1}|{rid}"))
        if page < pages - 1:
            nav.append(_btn("Next ▶", f"dl|sec|chap|{page + 1}|{rid}"))
        rows.append(nav)
    rows.append([_btn("← Back", f"dl|sec|edit|{rid}")])
    return InlineKeyboardMarkup(rows)


def prompt_text(field: str, duration: int, notice: str = "", section_number: int | None = None) -> str:
    which = "start" if field == "start" else "end"
    where = f" for section {section_number}" if section_number else ""
    lines = [f"Send the <b>{which}</b> time{where}.", ""]
    if notice:
        lines += [f"⚠ {esc(notice)}", ""]
    lines += [
        "Examples: <code>1:50:00</code>  ·  <code>50:00</code>  ·  <code>90</code> (seconds)",
        f"Length: {format_timestamp(duration)}",
    ]
    return "\n".join(lines)


def prompt_menu(field: str, index: int, rid: str) -> InlineKeyboardMarkup:
    empty_label = "Leave empty (from the start)" if field == "start" else "Leave empty (to the end)"
    return InlineKeyboardMarkup([
        [_btn(empty_label, f"dl|sec|clear|{field}|{index}|{rid}")],
        [_btn("← Back", f"dl|sec|edit|{rid}")],
    ])


def sections_summary(sections: list[tuple[float, float]], merge: bool) -> str:
    """The block added to the quality menu's caption while sections are chosen,
    so it is obvious that the buttons below download only these parts."""
    n = len(sections)
    lines = [f"✄ {n} section{'s' if n != 1 else ''} — only {'these' if n != 1 else 'this'} will be downloaded"]
    lines += [f"{i}. {format_section(a, b)}" for i, (a, b) in enumerate(sections[:MAX_SUMMARY_LINES], 1)]
    if n > MAX_SUMMARY_LINES:
        lines.append(f"+{n - MAX_SUMMARY_LINES} more")
    if n > 1:
        lines.append("Merged into one" if merge else "Separate clips")
    return "\n".join(lines)
