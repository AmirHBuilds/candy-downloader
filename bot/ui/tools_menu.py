"""
The toolbox screens: shown when someone sends the bot a video / audio file, and from Tools on the start menu.

Callback data "tl|<action>|...|<rid>" (rid last, like every other namespace):
  tl|home|<rid>            the toolbox for that file
  tl|trim|<rid>            ask for a start and end time (typed)          -> then tl|trimgo|fast|exact
  tl|trimgo|<mode>|<rid>   fast (copy) | exact (re-encode)
  tl|aud|<fmt>|<rid>       extract audio (mp3 | m4a)
  tl|cmpm|<rid>            the "fit in ... MB" screen
  tl|cmp|<mb>|<rid>        compress to that size
  tl|gif|<rid>             ask for start and length (typed)
  tl|strip|<rid>           remove metadata
  tl|burn|<rid>            ask for an .srt file, then burn it into the picture
  tl|x|<rid>               close (forget the file)
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from downloader.tools import COMPRESS_TARGETS_MB, MAX_GIF_SECONDS, MediaInfo
from utils.text import esc


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def clock(seconds: float | None) -> str:
    if not seconds:
        return "?"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def size_text(size: int) -> str:
    mb = size / 1_000_000
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"


def file_line(name: str, info: MediaInfo) -> str:
    parts = [clock(info.duration), size_text(info.size)]
    if info.has_video and info.width and info.height:
        parts.append(f"{info.width}×{info.height}")
    elif not info.has_video:
        parts.append("audio")
    shown = " ".join(name.split())
    return f"<i>{esc(shown[:60] + ('…' if len(shown) > 60 else ''))}</i>\n{' · '.join(parts)}"


# ------------------------------------------------------------------ the start-menu entry
TOOLS_INTRO = (
    "🧰 <b>Tools</b>\n\n"
    "Send me a video or audio file and I'll show what I can do with it:\n\n"
    "✄ <b>Trim</b> — keep just a part\n"
    "♪ <b>Extract audio</b> — MP3 or M4A\n"
    "⇩ <b>Compress</b> — make it fit 10 / 25 / 50 / 100 MB\n"
    "◍ <b>GIF</b> — a short looping clip\n"
    "◧ <b>Burn subtitles</b> — draw an .srt into the picture\n"
    "⌫ <b>Remove metadata</b> — title, location, encoder tags\n\n"
    "<i>Just send the file — no command needed.</i>"
)


def tools_intro_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("← Back", "nav|home")]])


# ------------------------------------------------------------------ the toolbox for one file
def toolbox_text(name: str, info: MediaInfo, notice: str = "") -> str:
    lines = ["🧰 <b>Toolbox</b>", file_line(name, info)]
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def toolbox_menu(info: MediaInfo, rid: str, burn_ok: bool = True) -> InlineKeyboardMarkup:
    rows = [[_btn("✄ Trim", f"tl|trim|{rid}")]]
    if info.has_audio:
        rows[0].append(_btn("♪ Extract audio", f"tl|aud|menu|{rid}"))
    if info.has_video:
        rows.append([_btn("⇩ Compress", f"tl|cmpm|{rid}"), _btn("◍ GIF", f"tl|gif|{rid}")])
        if burn_ok:
            rows.append([_btn("◧ Burn subtitles", f"tl|burn|{rid}")])
    rows.append([_btn("⌫ Remove metadata", f"tl|strip|{rid}")])
    rows.append([_btn("✕ Close", f"tl|x|{rid}")])
    return InlineKeyboardMarkup(rows)


def audio_text(name: str, info: MediaInfo) -> str:
    return "\n".join(["♪ <b>Extract audio</b>", file_line(name, info), "", "<i>M4A keeps more quality per MB; MP3 plays everywhere.</i>"])


def audio_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("MP3", f"tl|aud|mp3|{rid}"), _btn("M4A", f"tl|aud|m4a|{rid}")],
        [_btn("← Back", f"tl|home|{rid}")],
    ])


def compress_text(name: str, info: MediaInfo, notice: str = "") -> str:
    lines = ["⇩ <b>Compress</b>", file_line(name, info), "", "Make it fit in…",
             "<i>Smaller sizes lower the quality (and the resolution when needed). Quality is chosen automatically.</i>"]
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def compress_menu(info: MediaInfo, rid: str) -> InlineKeyboardMarkup:
    current_mb = info.size / 1_000_000
    buttons = [_btn(f"{mb} MB", f"tl|cmp|{mb}|{rid}") for mb in COMPRESS_TARGETS_MB if mb < current_mb * 0.9]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_btn("← Back", f"tl|home|{rid}")])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ typed input
def trim_prompt(info: MediaInfo, notice: str = "") -> str:
    lines = ["✄ <b>Trim</b>", f"Length: {clock(info.duration)}", "",
             "Send the <b>start</b> and <b>end</b> time, for example:",
             "<code>1:20 2:45</code>   or   <code>0:30-1:10</code>",
             "<i>Only a start (like 5:00) means “until the end”.</i>"]
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def trim_mode_text(start_text: str, end_text: str, has_video: bool) -> str:
    lines = ["✄ <b>Trim</b>", f"{esc(start_text)} → {esc(end_text)}", ""]
    if has_video:
        lines += ["<b>Fast</b> — instant and lossless, but a video may begin a moment early (nearest keyframe).",
                  "<b>Exact</b> — cuts precisely where you said; takes longer (re-encodes)."]
    else:
        lines.append("Cutting audio is instant and lossless.")
    return "\n".join(lines)


def trim_mode_menu(rid: str, has_video: bool) -> InlineKeyboardMarkup:
    if has_video:
        row = [_btn("⚡ Fast", f"tl|trimgo|fast|{rid}"), _btn("✄ Exact", f"tl|trimgo|exact|{rid}")]
    else:
        row = [_btn("✄ Trim", f"tl|trimgo|fast|{rid}")]
    return InlineKeyboardMarkup([row, [_btn("← Back", f"tl|trim|{rid}")]])


def gif_prompt(info: MediaInfo, notice: str = "") -> str:
    lines = ["◍ <b>GIF</b>", f"Length: {clock(info.duration)}", "",
             "Send the <b>start</b> time and, if you like, how many <b>seconds</b> (up to "
             f"{MAX_GIF_SECONDS}):",
             "<code>1:20 5</code>   — 5 seconds from 1:20", "<code>0:10</code>   — 5 seconds from 0:10"]
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def burn_prompt(info: MediaInfo, notice: str = "") -> str:
    lines = ["◧ <b>Burn subtitles</b>", f"Length: {clock(info.duration)}", "",
             "Send me the subtitle file (<code>.srt</code>) and I'll draw it into the picture.",
             "<i>Persian, Arabic and most other scripts work. This re-encodes the video, so it takes a while.</i>"]
    if notice:
        lines += ["", f"⚠ {esc(notice)}"]
    return "\n".join(lines)


def prompt_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("← Back", f"tl|home|{rid}")]])
