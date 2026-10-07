"""
Menus shown right after a link is detected. Quality buttons are built
from what the link *actually* offers (see downloader/probe.py) rather
than a fixed guess - a 720p-max video only shows 720p and below, and
sites with no real quality concept (galleries, direct files) skip
quality selection entirely.

Every callback_data here ends with a request token (rid) - a short id
unique to *this specific link/message*, not just the user. Without it,
sending a second link before acting on the first would silently make
the first message's buttons act on the second link instead (they'd
share one per-user slot) - this is what threading the token through
everywhere prevents.
"""
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from downloader.probe import ProbeResult
from downloader.music import MAX_TRACKS


def _sized(text: str, size_labels: dict | None, key: str) -> str:
    """'720p' -> '720p ~45MB' when an estimate exists and the person hasn't
    turned sizes off (callers then pass no labels at all)."""
    suffix = (size_labels or {}).get(key)
    return f"{text} {suffix}" if suffix else text


def cancel_row(rid: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("✕ Cancel", callback_data=f"dl|cancel|{rid}")]


def _clip_row(probe: ProbeResult | None, rid: str, section_count: int = 0) -> list[list[InlineKeyboardButton]]:
    """The "download only part of it" entry point. Offered only when the
    probe knows the length (never for playlists / live streams) - the editor
    validates timestamps against it. Once sections are chosen the label says
    so, because every download button on the menu will then use them."""
    if probe is None or not probe.duration:
        return []
    label = f"✄ Sections ({section_count}) ✓" if section_count else "✄ Add section"
    return [[InlineKeyboardButton(label, callback_data=f"dl|sec|open|{rid}")]]


def _subtitles_row(probe: ProbeResult | None, rid: str, section_count: int,
                   subtitle_count: int) -> list[list[InlineKeyboardButton]]:
    """Subtitles for a video. Not offered with time-range sections: yt-dlp downloads
    the subtitles for the WHOLE video, so on a clip they would be out of sync."""
    if probe is None or not probe.subtitles or section_count:
        return []
    label = f"◧ Subtitles ({subtitle_count}) ✓" if subtitle_count else "◧ Subtitles"
    return [[InlineKeyboardButton(label, callback_data=f"dl|sub|open|0|{rid}")]]


def _split_row(probe: ProbeResult | None, rid: str, section_count: int,
               size_labels: dict | None) -> list[list[InlineKeyboardButton]]:
    """One MP3 per chapter, for a long mix or an album in one video. Not offered
    while time-range sections are chosen: those already say which parts to take."""
    if probe is None or section_count or not (2 <= len(probe.chapters) <= MAX_TRACKS):
        return []
    return [[InlineKeyboardButton(_sized(f"♪ MP3 · {len(probe.chapters)} tracks", size_labels, "mp3"),
                                   callback_data=f"dl|audio|mp3split|{rid}")]]


def quality_menu(probe: ProbeResult, rid: str, section_count: int = 0,
                 size_labels: dict | None = None) -> InlineKeyboardMarkup:
    """The right first screen for a probed link: video picker, or - for an
    audio-only source - just the audio formats, or a single Download button.
    (section_count only matters for the audio-only menu, which has no
    "More options" screen to hold the sections button.)"""
    if probe.heights:
        return video_menu(probe, rid, size_labels)
    if probe.has_audio:
        return audio_only_menu(rid, probe, section_count, size_labels)
    return simple_menu(rid)


def video_menu(probe: ProbeResult, rid: str, size_labels: dict | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(_sized("★ Best available", size_labels, "best"),
                                   callback_data=f"dl|video|best|{rid}")]]

    shown = [h for h in probe.heights if h]
    picks = []
    for h in shown:
        if not picks or (picks[-1] - h) >= 120:
            picks.append(h)
        if len(picks) == 3:
            break
    if picks:
        rows.append([
            InlineKeyboardButton(_sized(f"{h}p", size_labels, str(h)), callback_data=f"dl|video|{h}p|{rid}")
            for h in picks
        ])

    if probe.has_audio:
        rows.append([
            InlineKeyboardButton(_sized("♪ MP3", size_labels, "mp3"), callback_data=f"dl|audio|mp3|{rid}"),
            InlineKeyboardButton(_sized("♪ Opus", size_labels, "opus"), callback_data=f"dl|audio|opus|{rid}"),
        ])

    rows.append([InlineKeyboardButton("More options…", callback_data=f"dl|moreq|{rid}")])
    rows.append(cancel_row(rid))
    return InlineKeyboardMarkup(rows)


def extended_video_menu(probe: ProbeResult, rid: str, section_count: int = 0,
                        size_labels: dict | None = None, subtitle_count: int = 0) -> InlineKeyboardMarkup:
    rows = []
    picks = []
    for h in probe.heights or []:
        if not picks or (picks[-1] - h) >= 60:
            picks.append(h)
    for i in range(0, len(picks), 3):
        rows.append([
            InlineKeyboardButton(_sized(f"{h}p", size_labels, str(h)), callback_data=f"dl|video|{h}p|{rid}")
            for h in picks[i:i + 3]
        ])
    rows.append([InlineKeyboardButton(_sized("↓ Smallest size", size_labels, "worst"),
                                       callback_data=f"dl|video|worst|{rid}")])
    rows.extend(_split_row(probe, rid, section_count, size_labels))
    rows.extend(_subtitles_row(probe, rid, section_count, subtitle_count))
    rows.extend(_clip_row(probe, rid, section_count))
    rows.append([InlineKeyboardButton("← Back", callback_data=f"dl|backq|{rid}")])
    return InlineKeyboardMarkup(rows)


def audio_only_menu(rid: str, probe: ProbeResult | None = None, section_count: int = 0,
                    size_labels: dict | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_sized("♪ MP3", size_labels, "mp3"), callback_data=f"dl|audio|mp3|{rid}"),
         InlineKeyboardButton(_sized("♪ Opus", size_labels, "opus"), callback_data=f"dl|audio|opus|{rid}")],
        *_split_row(probe, rid, section_count, size_labels),
        *_clip_row(probe, rid, section_count),
        cancel_row(rid),
    ])


def simple_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↓ Download", callback_data=f"dl|simple|download|{rid}")],
        cancel_row(rid),
    ])


def fallback_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("★ Best quality", callback_data=f"dl|video|best|{rid}")],
        [InlineKeyboardButton("↓ Smallest size", callback_data=f"dl|video|worst|{rid}"),
         InlineKeyboardButton("♪ Audio only", callback_data=f"dl|audio|mp3|{rid}")],
        cancel_row(rid),
    ])


def spotify_menu(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♪ Download MP3", callback_data=f"dl|audio|mp3|{rid}")],
        cancel_row(rid),
    ])


def link_row(url: str) -> list[InlineKeyboardButton]:
    """A tap-to-copy button for the original source link. Telegram deletes
    the person's own message once we've picked up its URL (see
    link_handler), and the status message that shows it as <code> text
    also gets deleted once the file is delivered - without this, the link
    is gone from the chat entirely the moment the download finishes."""
    # Telegram limits a copy-text payload to 256 characters - fall back to
    # a plain (non-copy) link-styled button rather than crashing if some
    # unusually long URL ever exceeds that.
    if len(url) > 256:
        return [InlineKeyboardButton("🔗 Video link", url=url)]
    return [InlineKeyboardButton("🔗 Video link", copy_text=CopyTextButton(text=url))]


def send_as_file_menu(rid: str, url: str, label: str = "▤ Send as file instead") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"dl|asfile|{rid}")],
        link_row(url),
    ])


def sent_menu(url: str) -> InlineKeyboardMarkup:
    """Attached to audio/document sends, which have no "send as file"
    choice of their own - just keeps the source link copyable."""
    return InlineKeyboardMarkup([link_row(url)])


def redo_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    """Cancelled before a quality pick was even made, so there's no
    completed download settings to retry - re-shows the quality picker
    instead. No Delete: see cancelled_menu for why."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("↻ Try again", callback_data=f"dl|redo|{rid}")] + link_row(url)])


def retry_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    """Used for every terminal non-success state (cancelled, failed,
    expired) so "Try again" and "Delete" are always both offered together
    - previously a couple of code paths built their own ad-hoc
    Try-again-only markup, so the delete button only showed up
    sometimes. Also the only place the source link survives on a
    cancelled/failed download, now that it's no longer inlined into the
    message text."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Try again", callback_data=f"dl|retry|{rid}"),
         InlineKeyboardButton("✕ Delete", callback_data=f"dl|dismiss|{rid}")],
        link_row(url),
    ])


# Same shape, kept as a separate name where the call site is specifically
# about a cancellation rather than a failure - purely for readability.
def cancelled_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    """Cancelled (as opposed to Failed - see retry_menu): just Try again
    and the link. No Delete button here - a Cancelled message already
    reads as "this went away"; a Failed one still looks like a live
    problem someone might want to clear, which is the difference."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("↻ Try again", callback_data=f"dl|retry|{rid}")] + link_row(url)])


def queued_menu(rid: str, url: str) -> InlineKeyboardMarkup:
    """Cancel only, deliberately - no copy-link button while a job's still
    in flight. It only makes sense once there's a final state to act on
    (see retry_menu/cancelled_menu and sent_menu/send_as_file_menu)."""
    del url  # kept in the signature so call sites don't need touching if this changes again
    return InlineKeyboardMarkup([cancel_row(rid)])
