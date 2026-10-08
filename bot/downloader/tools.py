"""
The media toolbox: ffmpeg operations on a file the person sent to the bot.

  trim      cut [start, end] - "fast" copies the streams (snaps to keyframes), "exact" re-encodes
  audio     extract the sound track as MP3 or M4A
  compress  shrink a video to fit a size (10 / 25 / 50 / 100 MB ...) by choosing a bitrate and, if needed, a height
  gif       a short silent animation (max 15 s)
  strip     remove metadata (title, GPS, encoder tags, chapters) without re-encoding
  burn      draw an .srt subtitle file into the picture (also used by "burned-in" subtitles on downloads)

Everything is plain ffmpeg. The argument builders are pure functions (tested without running anything); one
runner executes them with a progress bar, a cancel check and a timeout, like the downloaders do. Every error
raised here is a ToolError whose message is written for the person.
"""
import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from downloader.errors import JobCancelled
from downloader.sections import SectionError, parse_timestamp

log = logging.getLogger("candy.tools")

TOOL_TIMEOUT_SECONDS = 30 * 60
MAX_GIF_SECONDS = 15
COMPRESS_TARGETS_MB = (10, 25, 50, 100)
AUDIO_FORMATS = ("mp3", "m4a")
MIN_VIDEO_KBPS = 150
AUDIO_KBPS = 96
SIZE_SAFETY = 0.92          # container overhead + bitrate overshoot: aim a little under the target

# Subtitle look: big enough for a phone, outlined so it reads on any picture, a font that has Arabic/Persian.
BURN_STYLE = "FontName=Noto Sans,Fontsize=22,Outline=2,Shadow=0,MarginV=28,Alignment=2"

_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".3gp", ".flv"}
_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".wma"}


class ToolError(ValueError):
    """Shown to the person as-is (plain text)."""


@dataclass
class MediaInfo:
    duration: float | None = None
    has_video: bool = False
    has_audio: bool = False
    width: int | None = None
    height: int | None = None
    size: int = 0


def looks_like_media(name: str, mime: str | None) -> bool:
    """Is this document something the toolbox can work on?"""
    mime = (mime or "").lower()
    if mime.startswith(("video/", "audio/")):
        return True
    return Path(name or "").suffix.lower() in (_VIDEO_EXTS | _AUDIO_EXTS)


async def probe(path: Path) -> MediaInfo:
    """Duration, streams and size of a file. Raises ToolError if ffprobe can't read it as media."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,width,height",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except (OSError, ValueError):
        raise ToolError("I couldn't read that file.") from None
    streams = data.get("streams") or []
    if not streams:
        raise ToolError("That doesn't look like a video or audio file.")
    info = MediaInfo(size=path.stat().st_size if path.exists() else 0)
    try:
        info.duration = float(data.get("format", {}).get("duration")) or None
    except (TypeError, ValueError):
        info.duration = None
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.width, info.height = stream.get("width"), stream.get("height")
        elif kind == "audio":
            info.has_audio = True
    return info


# ---------------------------------------------------------------- typed input (pure)
_DASHES = str.maketrans({"–": " ", "—": " ", "-": " ", "→": " ", "،": " ", ",": " "})


def _tokens(text: str) -> list[str]:
    cleaned = (text or "").translate(_DASHES)
    cleaned = re.sub(r"\bto\b", " ", cleaned, flags=re.IGNORECASE)
    return cleaned.split()


def _time(token: str) -> float:
    try:
        return parse_timestamp(token)
    except SectionError as exc:
        raise ToolError(str(exc)) from None


def parse_range(text: str, duration: float | None) -> tuple[float, float]:
    """'1:20 2:45', '0:30-1:10', or just '5:00' (to the end) -> (start, end) in seconds."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > 2:
        raise ToolError("Send a start and an end time, like 1:20 2:45.")
    start = _time(tokens[0])
    end = _time(tokens[1]) if len(tokens) == 2 else (duration or 0)
    if duration:
        if start >= duration:
            raise ToolError(f"The start is past the end of the file (it is {int(duration)} seconds long).")
        end = min(end, duration)
    if end <= start:
        raise ToolError("The end must be after the start.")
    return start, end


def parse_gif(text: str, duration: float | None, default_seconds: float = 5) -> tuple[float, float]:
    """'1:20 5' -> (80, 5); '1:20' -> (80, 5). The length is capped at MAX_GIF_SECONDS and at the file's end."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > 2:
        raise ToolError("Send a start time and, if you like, a number of seconds, like 1:20 5.")
    start = _time(tokens[0])
    length = _time(tokens[1]) if len(tokens) == 2 else default_seconds
    if length <= 0:
        raise ToolError("The length must be above zero.")
    if length > MAX_GIF_SECONDS:
        raise ToolError(f"A GIF can be at most {MAX_GIF_SECONDS} seconds.")
    if duration:
        if start >= duration:
            raise ToolError(f"The start is past the end of the file (it is {int(duration)} seconds long).")
        length = min(length, duration - start)
    return start, length


def normalize_srt(path: Path) -> None:
    """Rewrite a subtitle file as UTF-8. Old Persian/Arabic subtitle files are often Windows-1256 or UTF-16;
    ffmpeg reads them as UTF-8 and shows garbage."""
    raw = path.read_bytes()
    if not raw.strip():
        raise ToolError("That subtitle file is empty.")
    text = None
    # UTF-16 only with its byte-order mark (without one, other encodings decode as "valid" garbage).
    candidates = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1256", "cp1252")
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None or "-->" not in text:
        raise ToolError("That doesn't look like an .srt subtitle file.")
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")


def burn_allowed(duration: float | None) -> bool:
    """Burning subtitles re-encodes the whole video: refuse the very long ones (BURN_MAX_SECONDS)."""
    from config import BURN_MAX_SECONDS
    return not duration or duration <= BURN_MAX_SECONDS


# ---------------------------------------------------------------- argument builders (pure)
def _seconds(value: float) -> str:
    return f"{value:.3f}"


def trim_args(src: Path, dst: Path, start: float, end: float, exact: bool, has_video: bool) -> list[str]:
    if end <= start:
        raise ToolError("The end must be after the start.")
    if not exact or not has_video:
        # Stream copy: instant and lossless; a video then starts at the nearest keyframe before `start`.
        return ["-ss", _seconds(start), "-to", _seconds(end), "-i", str(src), "-map", "0:v?", "-map", "0:a?",
                "-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)]
    return ["-ss", _seconds(start), "-to", _seconds(end), "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(dst)]


def audio_args(src: Path, dst: Path, fmt: str) -> list[str]:
    if fmt == "mp3":
        return ["-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k", str(dst)]
    if fmt == "m4a":
        return ["-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
    raise ToolError("Unknown audio format.")


def plan_compress(duration: float | None, target_mb: float, src_height: int | None) -> tuple[int, int | None]:
    """(video kbit/s, scale-to height or None) so the result is about target_mb. Raises ToolError when the
    video is too long to fit at a watchable quality."""
    if not duration or duration <= 0:
        raise ToolError("I can't tell how long this video is, so I can't aim for a size.")
    total_kbps = target_mb * 1_000_000 * 8 * SIZE_SAFETY / duration / 1000
    video_kbps = int(total_kbps - AUDIO_KBPS)
    if video_kbps < MIN_VIDEO_KBPS:
        raise ToolError(f"This video is too long to fit in {target_mb:g} MB at a watchable quality. "
                        f"Pick a bigger size, or trim it first.")
    # Fewer pixels per second of budget -> less blocky: step the height down as the budget shrinks.
    height = None
    for limit, cap in ((350, 360), (700, 480), (1400, 720), (2800, 1080)):
        if video_kbps < limit:
            height = cap
            break
    if height and src_height and src_height <= height:
        height = None                                      # never scale UP
    return video_kbps, height


def compress_args(src: Path, dst: Path, video_kbps: int, height: int | None) -> list[str]:
    args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.25)}k", "-bufsize", f"{video_kbps * 2}k"]
    if height:
        args += ["-vf", f"scale=-2:{height}"]
    args += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ac", "2", "-movflags", "+faststart", str(dst)]
    return args


def gif_args(src: Path, dst: Path, start: float, length: float) -> list[str]:
    if length <= 0:
        raise ToolError("The GIF needs a length above zero.")
    if length > MAX_GIF_SECONDS:
        raise ToolError(f"A GIF can be at most {MAX_GIF_SECONDS} seconds.")
    return ["-ss", _seconds(start), "-t", _seconds(length), "-i", str(src), "-an",
            "-vf", "fps=12,scale='min(480,iw)':-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=bayer:bayer_scale=4",
            "-loop", "0", str(dst)]


def strip_args(src: Path, dst: Path) -> list[str]:
    return ["-i", str(src), "-map", "0:v?", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-c", "copy", str(dst)]


def burn_args(src: Path, srt_name: str, dst: Path) -> list[str]:
    """`srt_name` is a bare file name that sits in the working directory ffmpeg is started in: file names
    inside a filter need awkward escaping (colons, quotes, commas), so the runner avoids paths entirely."""
    return ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-vf", f"subtitles={srt_name}:force_style='{BURN_STYLE}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(dst)]


# ---------------------------------------------------------------- the runner
_OUT_TIME = re.compile(r"^out_time_(?:us|ms)=(\d+)")      # both are microseconds in practice
_SPEED = re.compile(r"^speed=\s*([\d.]+)x")


async def run_ffmpeg(args: list[str], *, duration: float | None, cb, cancel_event: asyncio.Event | None,
                     stage: str, cwd: Path | None = None) -> None:
    """Run ffmpeg with a progress report (cb(percent, speed, eta, stage)), cancel and timeout.
    Raises JobCancelled on cancel, ToolError (with ffmpeg's own complaint) on failure."""
    cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *args]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                cwd=str(cwd) if cwd else None)
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def pump() -> None:
        speed = None
        last = 0.0
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            m = _SPEED.match(text)
            if m:
                speed = float(m.group(1))
                continue
            m = _OUT_TIME.match(text)
            if m and duration:
                done = int(m.group(1)) / 1_000_000
                percent = max(0.0, min(99.0, done / duration * 100))
                now = time.monotonic()
                if now - last >= 1.0:
                    last = now
                    eta = None
                    if speed and speed > 0:
                        remaining = max(0.0, (duration - done) / speed)
                        eta = f"{int(remaining // 60)}:{int(remaining % 60):02d}"
                    cb(percent, f"{speed:.1f}x" if speed else None, eta, stage)

    pump_task = asyncio.create_task(pump())
    started = time.monotonic()
    interrupted = None
    while not pump_task.done():
        await asyncio.wait({pump_task}, timeout=0.5)
        if cancel_event is not None and cancel_event.is_set():
            interrupted = "cancel"
        elif time.monotonic() - started > TOOL_TIMEOUT_SECONDS:
            interrupted = "timeout"
        if interrupted:
            try:
                proc.kill()
            except ProcessLookupError:
                pass                                  # it finished in the instant between the check and the kill
            break
    await asyncio.wait({pump_task})
    await proc.wait()
    err = (await stderr_task).decode("utf-8", "replace").strip()
    if interrupted == "cancel":
        raise JobCancelled("Cancelled by user")
    if interrupted == "timeout":
        raise ToolError("That took too long and was stopped.")
    if proc.returncode:
        tail = " ".join(err.splitlines()[-2:])[-250:] or f"ffmpeg exited with code {proc.returncode}"
        log.warning("ffmpeg failed (%s): %s", stage, err[-600:])
        raise ToolError(f"ffmpeg couldn't do that: {tail}")
    cb(100.0, None, None, stage)


# ---------------------------------------------------------------- one entry point for the job manager
def _safe_stem(name: str) -> str:
    stem = Path(name or "file").stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .") or "file"
    return stem[:80]


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}-{seconds % 3600 // 60:02d}-{seconds % 60:02d}" if seconds >= 3600 else \
        f"{seconds // 60}-{seconds % 60:02d}"


async def run(settings: dict, workspace: Path, cb, cancel_event: asyncio.Event | None) -> list[Path]:
    """Execute the tool described by a job's settings; returns the output files (inside `workspace`)."""
    src = Path(settings.get("tool_input") or "")
    if not src.is_file():
        raise ToolError("That file isn't available any more - please send it again.")
    tool = settings.get("tool")
    stem = _safe_stem(settings.get("tool_name") or src.name)
    suffix = src.suffix.lower() or ".mp4"
    duration = settings.get("tool_duration")
    has_video = bool(settings.get("tool_has_video", True))
    height = settings.get("tool_height")

    if tool == "trim":
        start, end = float(settings["start"]), float(settings["end"])
        exact = bool(settings.get("exact")) and has_video
        out_suffix = ".mp4" if exact else suffix
        dst = workspace / f"{stem} [{_fmt(start)}–{_fmt(end)}]{out_suffix}"
        await run_ffmpeg(trim_args(src, dst, start, end, exact, has_video), duration=end - start, cb=cb,
                         cancel_event=cancel_event, stage="Cutting" if exact else "Trimming")
    elif tool == "audio":
        fmt = settings.get("audio_format", "mp3")
        dst = workspace / f"{stem}.{fmt}"
        await run_ffmpeg(audio_args(src, dst, fmt), duration=duration, cb=cb, cancel_event=cancel_event,
                         stage="Extracting audio")
    elif tool == "compress":
        kbps, scale_to = plan_compress(duration, float(settings["target_mb"]), height)
        dst = workspace / f"{stem} [{settings['target_mb']:g}MB].mp4"
        await run_ffmpeg(compress_args(src, dst, kbps, scale_to), duration=duration, cb=cb, cancel_event=cancel_event,
                         stage="Compressing")
    elif tool == "gif":
        start, length = float(settings["start"]), float(settings["length"])
        dst = workspace / f"{stem}.gif"
        await run_ffmpeg(gif_args(src, dst, start, length), duration=length, cb=cb, cancel_event=cancel_event,
                         stage="Making the GIF")
    elif tool == "strip":
        dst = workspace / f"{stem} [clean]{suffix}"
        await run_ffmpeg(strip_args(src, dst), duration=duration, cb=cb, cancel_event=cancel_event,
                         stage="Removing metadata")
    elif tool == "burn":
        srt = Path(settings.get("tool_srt") or "")
        if not srt.is_file():
            raise ToolError("The subtitle file isn't available any more - please send it again.")
        dst = await burn_into(src, srt, workspace, duration, cb, cancel_event, stem=stem)
    else:
        raise ToolError("Unknown tool.")
    if not dst.is_file() or dst.stat().st_size == 0:
        raise ToolError("That produced an empty file.")
    return [dst]


async def burn_into(video: Path, srt: Path, workspace: Path, duration: float | None, cb,
                    cancel_event: asyncio.Event | None, *, stem: str | None = None) -> Path:
    """Draw `srt` into `video`; the result is a new mp4 in `workspace`. Used by the toolbox and by downloads."""
    local_srt = workspace / "burn_subs.srt"
    shutil.copyfile(srt, local_srt)                         # a bare name in ffmpeg's cwd: no filter escaping
    dst = workspace / f"{stem or _safe_stem(video.name)} [subtitled].mp4"
    try:
        await run_ffmpeg(burn_args(video, local_srt.name, dst), duration=duration, cb=cb, cancel_event=cancel_event,
                         stage="Burning subtitles", cwd=workspace)
    finally:
        local_srt.unlink(missing_ok=True)
    return dst


# ---------------------------------------------------------------- files waiting for a tool
INPUT_TTL_SECONDS = 60 * 60


@dataclass
class StoredInput:
    user_id: int
    path: Path
    name: str
    info: MediaInfo
    at: float
    srt: Path | None = None
    extra: dict = field(default_factory=dict)


class InputStore:
    """The files people sent, kept on disk while they choose a tool. Keyed by the request id (rid) that the
    toolbox buttons carry. Removed after a successful job, when the person walks away (TTL), and at startup
    (the folder lives under TMP_DIR, which is swept)."""

    def __init__(self, root: Path, ttl: float = INPUT_TTL_SECONDS, clock=time.time):
        self.root, self.ttl, self._clock = Path(root), ttl, clock
        self._items: dict[str, StoredInput] = {}

    def folder(self, rid: str) -> Path:
        folder = self.root / rid
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def add(self, rid: str, user_id: int, path: Path, name: str, info: MediaInfo) -> StoredInput:
        self.expire()
        item = StoredInput(user_id, Path(path), name, info, self._clock())
        self._items[rid] = item
        return item

    def get(self, rid: str, user_id: int) -> StoredInput | None:
        self.expire()
        item = self._items.get(rid)
        if item is None or item.user_id != user_id or not item.path.is_file():
            return None
        return item

    def discard(self, rid: str) -> None:
        self._items.pop(rid, None)
        shutil.rmtree(self.root / rid, ignore_errors=True)

    def expire(self) -> None:
        now = self._clock()
        for rid in [r for r, item in self._items.items() if now - item.at > self.ttl]:
            self.discard(rid)

    def for_user(self, user_id: int) -> list[str]:
        return [rid for rid, item in self._items.items() if item.user_id == user_id]


def _make_store() -> InputStore:
    from config import TMP_DIR
    return InputStore(Path(TMP_DIR) / "tools")


store = _make_store()
