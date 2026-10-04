"""
Audio done properly: correct tags, a square cover, and splitting a long mix
into one file per chapter.

yt-dlp already embeds the thumbnail and some metadata, but the results are
often poor for music: the cover is the 16:9 video frame (players crop or
stretch it), and an ordinary upload called "Artist - Song (Official Video)"
ends up with the whole string as the title and the channel as the artist.

Everything here works on the finished file with ffmpeg and never makes a
download fail: if a step can't run, the file from yt-dlp is used as it is.
"""
import asyncio
import logging
import re
import time
from pathlib import Path

from downloader.errors import JobCancelled

log = logging.getLogger("candy.music")

MAX_TRACKS = 50              # sending more separate files than this just trips Telegram's flood limits
FFMPEG_TIMEOUT = 600

# "(Official Video)", "[Lyrics]", "(HD)", "(Remastered 2011)" ... at the end of a title
_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:official(?:\s+\w+){0,3}|lyrics?(?:\s+video)?|audio|video|music\s+video|visuali[sz]er|"
    r"hd|hq|4k|remaster(?:ed)?[^\)\]]*)\s*[\)\]]\s*$",
    re.IGNORECASE,
)
_ARTIST_DASH_TITLE = re.compile(r"^(.+?)\s+[-–—]\s+(.+)$")
_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_title(title: str) -> str:
    title = " ".join((title or "").split())
    while True:
        stripped = _NOISE.sub("", title).strip()
        if stripped == title:
            return title
        title = stripped


def music_tags(info: dict) -> dict[str, str]:
    """{"title", "artist", "album", "date"} (only the ones we know) from yt-dlp's info.

    1. YouTube Music / auto-generated "Topic" uploads carry real track, artist
       and album fields - use them.
    2. Otherwise a title shaped "Artist - Song" is split (after removing
       "(Official Video)" style noise). A title that merely contains a dash
       ("Song - Live at X") is misread as Artist - Song; that is the trade-off
       of guessing, and the result is still a better default than the full
       string as the title.
    3. Otherwise the channel is the artist and the title is just cleaned."""
    track, artist = (info.get("track") or "").strip(), (info.get("artist") or "").strip()
    uploader = (info.get("uploader") or info.get("channel") or "").strip()
    if uploader.endswith(" - Topic"):
        uploader = uploader[: -len(" - Topic")]

    if track and artist:
        title, who = track, artist
    else:
        cleaned = clean_title(info.get("title") or "")
        match = _ARTIST_DASH_TITLE.match(cleaned)
        title, who = (match.group(2).strip(), match.group(1).strip()) if match else (cleaned, uploader)

    year = str(info.get("release_year") or (info.get("upload_date") or "")[:4] or "")
    tags = {"title": title, "artist": who, "album": (info.get("album") or "").strip(), "date": year}
    return {key: value for key, value in tags.items() if value}


def track_filename(number: int, title: str, suffix: str) -> str:
    """'01 - Some title.mp3' - safe on every filesystem, a sane length."""
    cleaned = " ".join(_BAD_FILENAME_CHARS.sub(" ", title or "").split())[:80].rstrip(" .") or "Track"
    return f"{number:02d} - {cleaned}{suffix}"


def valid_chapters(info: dict, duration: float | None = None) -> list[tuple[str, float, float]]:
    """[(title, start, end)] from yt-dlp's chapter list, with unusable entries dropped."""
    total = float(duration or info.get("duration") or 0) or None
    chapters = []
    for raw in info.get("chapters") or []:
        try:
            start = float(raw["start_time"])
            end = float(raw.get("end_time") or total or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if total:
            end = min(end, total)
        if end - start >= 1:
            chapters.append(((raw.get("title") or "").strip() or f"Track {len(chapters) + 1}", start, end))
    return chapters


# ---------------------------------------------------------------- ffmpeg
async def _ffmpeg(args: list[str], cancel_event: asyncio.Event | None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    waiter = asyncio.create_task(proc.communicate())
    started = time.monotonic()
    while not waiter.done():
        await asyncio.wait({waiter}, timeout=0.5)
        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled or time.monotonic() - started > FFMPEG_TIMEOUT:
            try:
                proc.kill()
            except ProcessLookupError:
                pass                        # it finished in the instant between the check and the kill
            await waiter
            if cancelled:
                raise JobCancelled("Cancelled by user")
            return 1, "timed out"
    _, err = waiter.result()
    return proc.returncode or 0, err.decode("utf-8", "replace")[-300:]


def _metadata_args(tags: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in tags.items():
        args += ["-metadata", f"{key}={value}"]
    return args


async def polish_mp3(path: Path, tags: dict[str, str], cancel_event: asyncio.Event | None = None) -> bool:
    """Write the tags and make the cover square (centre crop). The audio is
    copied untouched. True on success; on any failure the original is left alone."""
    temp = path.with_name(path.stem + ".polished.mp3")
    code, err = await _ffmpeg([
        "-i", str(path), "-map", "0:a", "-map", "0:v?",
        "-c:a", "copy", "-c:v", "mjpeg", "-q:v", "2",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
        "-id3v2_version", "3", *_metadata_args(tags),
        "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
        "-disposition:v", "attached_pic", str(temp),
    ], cancel_event)
    if code != 0 or not temp.exists() or temp.stat().st_size == 0:
        log.info("Could not polish %s: %s", path.name, err.strip())
        temp.unlink(missing_ok=True)
        return False
    temp.replace(path)
    return True


async def split_by_chapters(source: Path, chapters: list[tuple[str, float, float]], base_tags: dict[str, str],
                            cancel_event: asyncio.Event | None = None) -> list[Path]:
    """One file per chapter, cut from the already-downloaded audio without
    re-encoding (so it is fast and lossless). Each track is tagged with its
    chapter title and "n/N", sharing the album and artist, and keeps the cover.
    Returns [] if anything fails, so the caller can fall back to the whole file."""
    suffix = source.suffix
    total = len(chapters)
    made: list[Path] = []
    for number, (title, start, end) in enumerate(chapters, 1):
        target = source.with_name(track_filename(number, title, suffix))
        counter = 2
        while target.exists():
            target = source.with_name(f"{target.stem} ({counter}){suffix}")
            counter += 1
        tags = {**base_tags, "title": title, "track": f"{number}/{total}"}
        # mp3: start the tags clean (the whole file's description/comment would be copied onto every track).
        # opus keeps the global block, because that is where the cover art lives.
        strip = ["-map_metadata", "-1"] if suffix == ".mp3" else []
        extra = ["-id3v2_version", "3"] if suffix == ".mp3" else []
        code, err = await _ffmpeg([
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
            "-map", "0", "-c", "copy", *strip, *extra, *_metadata_args(tags), str(target),
        ], cancel_event)
        if code != 0 or not target.exists() or target.stat().st_size == 0:
            log.info("Could not cut chapter %d of %s: %s", number, source.name, err.strip())
            for leftover in [*made, target]:
                leftover.unlink(missing_ok=True)
            return []
        made.append(target)
    return made
