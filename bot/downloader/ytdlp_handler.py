import asyncio
import contextvars
import logging
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Callable

import yt_dlp

import config
from config import DATA_DIR, BGUTIL_POT_URL
from downloader import tools
from downloader.cookies import apply_cookies, cookie_file_for
from downloader.errors import JobCancelled
from downloader.proxy import policy, site_label
from downloader.music import MAX_TRACKS, clean_title, music_tags, polish_mp3, split_by_chapters, valid_chapters
from utils.procs import kill_processes_under
from downloader.sections import SectionError, filename_tag, format_section, validate_sections

log = logging.getLogger("candy.ytdlp")

_CAPTURE: contextvars.ContextVar = contextvars.ContextVar("ytdlp_capture", default=None)
_FFMPEG_HINTS = ("ffmpeg", "error", "invalid", "unable", "no such", "conversion", "codec", "failed", "unknown encoder")
_LONG_URL = re.compile(r"https?://\S{60,}")
_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")


def _scrub(line: str) -> str:
    """For logs people paste around: no signed media URLs, no user:password@ in proxy URLs."""
    return _CREDENTIALS.sub("", _LONG_URL.sub("<url>", str(line)))


class _Capture:
    """yt-dlp's own log, kept in memory. Its one-line error ("Conversion failed!") hides ffmpeg's
    real complaint, which only appears in the verbose log - so run verbose, keep the last few
    hundred lines, and print only the relevant ones when something goes wrong."""

    def __init__(self):
        self.lines: deque = deque(maxlen=300)

    def debug(self, message):
        self.lines.append(str(message))

    info = debug

    def warning(self, message):
        self.lines.append(str(message))
        log.info("yt-dlp warning: %s", _scrub(message))

    def error(self, message):
        self.lines.append(str(message))
        log.warning("yt-dlp: %s", _scrub(message))

    def tail(self, limit: int = 12) -> list[str]:
        relevant = [_scrub(line) for line in self.lines if any(h in line.lower() for h in _FFMPEG_HINTS)]
        return relevant[-limit:]

# percent, speed, eta, stage-label-override (used during post-processing,
# when yt-dlp's own download-progress hook goes silent for a while)
ProgressCB = Callable[[float, str | None, str | None, str | None], None]

# A genuinely hung ffmpeg/postprocessor step (rare, but possible) would
# otherwise freeze the UI forever with no error - bound it instead.
DOWNLOAD_TIMEOUT_SECONDS = 20 * 60

# yt-dlp reports each postprocessor by its short key ("Metadata", "MoveFiles",
# "ExtractAudio"), not its class name - the table used to hold class names
# ("FFmpegMetadata"), so those steps showed up as raw "Metadata"/"MoveFiles".
# Both spellings are kept in case a yt-dlp version reports the long one.
_POSTPROCESSOR_LABELS = {
    "Merger": "Merging video & audio",
    "VideoConvertor": "Converting video",
    "FFmpegVideoConvertor": "Converting video",
    "ExtractAudio": "Extracting audio",
    "FFmpegExtractAudio": "Extracting audio",
    "EmbedThumbnail": "Embedding thumbnail",
    "Metadata": "Adding metadata",
    "FFmpegMetadata": "Adding metadata",
    "MoveFiles": "Moving file",
    "EmbedSubtitle": "Embedding subtitles",
    "FFmpegEmbedSubtitle": "Embedding subtitles",
    "SponsorBlock": "Checking for sponsor segments",
    "ModifyChapters": "Removing sponsor segments",
}


def _postprocessor_label(name: str) -> str:
    if name.startswith(("Fixup", "FFmpegFixup")):
        return "Fixing up file"
    return _POSTPROCESSOR_LABELS.get(name, name or "Finishing up...")


def _stream_label(info: dict, settings: dict) -> str:
    """Which stream a progress event belongs to. yt-dlp usually fetches a
    video-only and an audio-only format separately, then merges them."""
    if settings.get("mode") == "audio":
        return "Audio"   # even if the site only offers a combined stream
    has_video = info.get("vcodec") not in (None, "none")
    has_audio = info.get("acodec") not in (None, "none")
    if has_audio and not has_video:
        return "Audio"
    if has_video:
        return "Video"
    return "Audio" if settings.get("mode") == "audio" else "Video"


def _fmt_speed(bytes_per_sec) -> str | None:
    if not bytes_per_sec:
        return None
    if bytes_per_sec >= 1_000_000:
        return f"{bytes_per_sec / 1_000_000:.1f}MB/s"
    return f"{bytes_per_sec / 1000:.0f}KB/s"


def _rename_opus_ogg(f: Path) -> Path:
    if f.suffix.lower() != ".ogg":
        return f
    target = f.with_suffix(".opus")
    try:
        f.rename(target)
        return target
    except OSError:
        return f  # not fatal - worst case the file just keeps its .ogg name


def _format_selector(s: dict) -> str:
    if s["mode"] == "audio":
        return "bestaudio/best"

    quality = s["quality"]
    if quality == "worst":
        return "worstvideo+worstaudio/worst"

    # Prefer an H.264+AAC pairing first - it remuxes into mp4 with zero
    # re-encoding and zero surprises. VP9/AV1 video paired with Opus
    # audio (yt-dlp's other common combo, especially once you cap the
    # height below the site's absolute best) doesn't fit cleanly in an
    # mp4 container; yt-dlp then keeps it as mkv instead despite
    # merge_output_format, which is what made "send as file" behave
    # inconsistently between quality picks - the cached/sent file simply
    # wasn't the plain mp4 the rest of the pipeline expected. Falling
    # back to "whatever's available" keeps every video downloadable even
    # when no h264 rendition exists at that height.
    if quality == "best":
        return (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "bestvideo+bestaudio/best"
        )
    height = quality.rstrip("p")
    return (
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    )


def _build_opts(url: str, workspace: Path, s: dict, user_id: int, progress_hook, pp_hook) -> dict:
    outtmpl = str(workspace / s["filename_template"])

    opts: dict = {
        "outtmpl": outtmpl,
        "format": _format_selector(s),
        "noplaylist": s["playlist_mode"] == "single",
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": max(1, int(s["concurrent_fragments"])),
        "retries": 5,
        "postprocessors": [],
    }

    if BGUTIL_POT_URL:
        # Lets yt-dlp fetch a PO Token from our companion container instead
        # of needing cookies - fixes "sign in to confirm you're not a bot"
        # for most YouTube links automatically.
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [BGUTIL_POT_URL]},
        }

    if s["playlist_mode"] == "range" and s["playlist_range"]:
        opts["playlist_items"] = s["playlist_range"]

    if s["rate_limit_kbps"]:
        opts["ratelimit"] = int(s["rate_limit_kbps"]) * 1024

    if s["proxy"]:
        opts["proxy"] = s["proxy"]

    cookie_path = cookie_file_for(user_id, s)
    if cookie_path:
        apply_cookies(opts, cookie_path)

    if s["use_archive"]:
        opts["download_archive"] = str(Path(DATA_DIR) / f"archive_{user_id}.txt")

    if s["mode"] == "audio":
        pp: dict = {"key": "FFmpegExtractAudio", "preferredcodec": s["audio_format"]}
        if s["audio_format"] not in ("flac", "wav"):
            # A bare number like "192" is ambiguous - for non-mp3 codecs
            # ffmpeg can misread it as a VBR quality scale (0-10) instead
            # of a bitrate, which fails outright for some codecs (opus
            # included - this was the actual cause of "Conversion failed!").
            # An explicit "192K" removes the ambiguity. Lossless formats
            # (flac/wav) don't take a bitrate at all, so skip it there.
            quality = str(s["audio_bitrate"])
            if not quality.lower().endswith("k"):
                quality = f"{quality}K"
            pp["preferredquality"] = quality
        opts["postprocessors"].append(pp)
    else:
        opts["merge_output_format"] = "mp4"

    sub_langs = s.get("sub_langs") or []
    if sub_langs and s["mode"] == "video":
        mode = s.get("sub_mode", "embed")
        # Human-made subtitles where they exist, YouTube's auto-generated ones otherwise (per language).
        opts.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=list(sub_langs),
                    subtitlesformat="srt/vtt/best")
        chain = [{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}]   # one format everywhere
        if mode in ("embed", "both"):
            # already_have_subtitle keeps the .srt files next to the video ("both"); otherwise they are removed once embedded.
            chain.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": mode == "both"})
        opts["postprocessors"][0:0] = chain

    if s["embed_thumbnail"]:
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail"})

    if s["embed_metadata"]:
        opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})

    if s["subtitles"] != "off":
        opts["writesubtitles"] = s["subtitles"] == "manual"
        opts["writeautomaticsub"] = s["subtitles"] == "auto"
        opts["subtitleslangs"] = [x.strip() for x in s["subtitle_langs"].split(",") if x.strip()]
        if s["embed_subtitles"]:
            opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

    if s["sponsorblock"]:
        opts["postprocessors"].append({
            "key": "SponsorBlock",
            "categories": ["sponsor"],
        })
        opts["postprocessors"].append({
            "key": "ModifyChapters",
            "remove_sponsor_segments": ["sponsor"],
        })

    capture = _CAPTURE.get()
    if capture is not None:
        opts["logger"] = capture
        opts["verbose"] = True            # goes to capture.debug, never to the console
    return opts


# Player clients to try, in order, when the default (web) client hits a
# "sign in to confirm you're not a bot" wall. YouTube scrutinizes its web
# client the hardest; tv/android/ios are checked far less and frequently
# work with zero login at all. This is the standard first-line fix used
# across the yt-dlp ecosystem - tried *before* falling back to cookies.
CLIENT_FALLBACKS = ["tv", "android", "ios"]

_SIGN_IN_MARKERS = ("sign in", "confirm you", "not a bot")


def _looks_like_bot_check(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _SIGN_IN_MARKERS)


def _add_note(settings: dict, text: str) -> None:
    """Something to tell the person once the files have been delivered (job_manager sends these)."""
    settings.setdefault("delivery_notes", []).append(text)


def _is_postprocessing_failure(exc: Exception) -> bool:
    # yt-dlp reports these as "ERROR: Postprocessing: <what ffmpeg said>". Deliberately NOT a loose
    # "postprocessing"/"ffmpeg" match: our own timeout message says "postprocessing step", and a
    # timeout must not start a chain of retries that could each take as long again.
    text = str(exc).lower()
    return "postprocessing:" in text or "conversion failed" in text


def _clear(workspace: Path) -> None:
    for leftover in workspace.iterdir():
        shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink(missing_ok=True)


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                   progress_cb: ProgressCB, cancel_event: asyncio.Event | None = None) -> list[Path]:
    """The real entry point: picks the route (direct, or via the proxy when the site is blocking this
    server's address - see downloader/proxy.py) and retries once on a different route if the first
    one is refused. Someone's own proxy setting bypasses all of it."""
    if settings.get("proxy"):
        return await _download_resilient(url, workspace, settings, user_id, progress_cb, cancel_event)
    proxy = policy.route(url)
    for attempt in (1, 2):
        trial = {**settings, "proxy": proxy} if proxy else settings
        try:
            files = await _download_resilient(url, workspace, trial, user_id, progress_cb, cancel_event)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and policy.report_failure(url, proxy, str(exc)):
                progress_cb(None, None, None, f"Switching route - {site_label(url)} blocked this address")
                _clear(workspace)
                proxy = policy.route(url)
                continue
            raise
        if trial is not settings:
            for note in trial.get("delivery_notes", []):
                _add_note(settings, note)
        policy.report_success(url, proxy)
        return files


async def _download_resilient(url: str, workspace: Path, settings: dict, user_id: int,
                              progress_cb: ProgressCB, cancel_event: asyncio.Event | None = None) -> list[Path]:
    """The download itself is the point; cover art, subtitles and format conversion are
    extras, so when a post-processing step fails the download is retried with fewer extras (and the person
    is told) rather than being thrown away. ffmpeg's real error is logged either way."""
    capture = _Capture()
    token = _CAPTURE.set(capture)
    try:
        try:
            return await _download_with_subtitles(url, workspace, settings, user_id, progress_cb, cancel_event)
        except JobCancelled:
            raise
        except Exception as first:  # noqa: BLE001
            if not _is_postprocessing_failure(first):
                raise
            log.warning("Post-processing failed for %s: %s | ffmpeg said: %s", url, first, capture.tail() or "(nothing captured)")
            fallbacks = []
            if settings.get("embed_thumbnail", True):
                fallbacks.append(({"embed_thumbnail": False},
                                  "◧ Cover art couldn't be added this time, so the file was sent without it."))
            if settings.get("mode") == "audio" and settings.get("audio_format") == "opus":
                fallbacks.append(({"embed_thumbnail": False, "audio_format": "mp3"},
                                  "◧ Opus conversion failed on this server, so this one is an MP3."))
            for override, note in fallbacks:
                _clear(workspace)
                try:
                    files = await _download_with_subtitles(url, workspace, {**settings, **override}, user_id,
                                                           progress_cb, cancel_event)
                except JobCancelled:
                    raise
                except Exception as again:  # noqa: BLE001
                    if not _is_postprocessing_failure(again):
                        raise
                    log.warning("Still failing with %s: %s | ffmpeg said: %s", override, again, capture.tail())
                    continue
                _add_note(settings, note)
                return files
            raise first
    finally:
        _CAPTURE.reset(token)


# YouTube rate-limits subtitle requests (HTTP 429) far more than video requests, and a shared address (WARP)
# makes it likelier. So a failed subtitle fetch is retried on its own, a little later, without touching the video.
SUBTITLE_RETRY_DELAYS = (0, 8, 25)


async def _download_with_subtitles(url: str, workspace: Path, settings: dict, user_id: int,
                                   progress_cb: ProgressCB, cancel_event: asyncio.Event | None = None) -> list[Path]:
    """Subtitles are the one thing that can break an otherwise fine download (YouTube answers 429 to the subtitle
    request, a postprocessor refuses). The video matters more: on any such failure it is downloaded again WITHOUT
    subtitles, and the subtitles get their own second chance (_recover_subtitles) before the person is told."""
    if not settings.get("sub_langs") or settings.get("sections"):    # (clips never carry subtitles)
        return await _download_main(url, workspace, settings, user_id, progress_cb, cancel_event)
    try:
        files = await _download_main(url, workspace, settings, user_id, progress_cb, cancel_event)
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        if _looks_like_bot_check(exc):
            raise                                    # not a subtitle problem
        log.warning("Download with subtitles failed (%s); fetching the video without them first", exc)
        _clear(workspace)
        plain = {k: v for k, v in settings.items() if k not in ("sub_langs", "sub_mode")}
        files = await _download_main(url, workspace, plain, user_id, progress_cb, cancel_event)
        return await _recover_subtitles(files, url, workspace, settings, user_id, progress_cb, cancel_event, str(exc))
    files = await _verify_subtitles(files, settings)
    if settings.get("sub_mode") in ("burn", "burnfile"):
        files = await _burn_subtitles(files, workspace, settings, progress_cb, cancel_event)
    return files


def _subtitle_failure_note(reason: str, partial: str = "") -> str:
    if "429" in reason or "too many requests" in reason.lower():
        why = "YouTube is limiting subtitle requests from this server right now (HTTP 429)"
    else:
        why = "the subtitles couldn't be fetched"
    return f"◧ {partial or 'The video was sent without subtitles'}: {why}. Try again in a few minutes."


async def _sleep(seconds: float, cancel_event: asyncio.Event | None) -> None:
    """Wait, but notice a cancel within half a second."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        _raise_if_cancelled(cancel_event)
        await asyncio.sleep(min(0.5, left))
    _raise_if_cancelled(cancel_event)


async def _subtitle_pass(url: str, workspace: Path, settings: dict, user_id: int, langs: list[str]) -> None:
    """One yt-dlp run that fetches ONLY the subtitles (no video). Errors for single languages are not raised:
    the caller looks at which files arrived."""
    opts = _build_opts(url, workspace, {**settings, "mode": "video", "sub_langs": list(langs)}, user_id,
                       lambda d: None, lambda d: None)
    opts.update(skip_download=True, ignoreerrors=True, ignore_no_formats_error=True, writesubtitles=True,
                writeautomaticsub=True, subtitleslangs=list(langs), subtitlesformat="srt/vtt/best",
                sleep_interval_subtitles=1, progress_hooks=[], postprocessor_hooks=[],
                postprocessors=[{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}])
    opts.pop("download_archive", None)       # the video was just archived: it would be skipped, subtitles and all

    def run() -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, run), timeout=120)


def _find_subtitle(workspace: Path, lang: str) -> Path | None:
    return next(iter(sorted(p for p in workspace.glob(f"*.{lang}.srt") if p.is_file())), None)


async def _fetch_subtitles(url: str, workspace: Path, settings: dict, user_id: int,
                           cancel_event: asyncio.Event | None) -> dict[str, Path]:
    """The subtitles, with retries on their own schedule (SUBTITLE_RETRY_DELAYS); only the missing languages are
    asked for again. Returns {language: .srt path} for what arrived - possibly nothing."""
    wanted = list(settings["sub_langs"])
    got: dict[str, Path] = {}
    for delay in SUBTITLE_RETRY_DELAYS:
        missing = [lang for lang in wanted if lang not in got]
        if not missing:
            break
        await _sleep(delay, cancel_event)
        try:
            await _subtitle_pass(url, workspace, settings, user_id, missing)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log.info("Subtitle fetch attempt failed: %s", exc)
        for lang in missing:
            path = _find_subtitle(workspace, lang)
            if path:
                got[lang] = path
    return got


async def _recover_subtitles(files: list[Path], url: str, workspace: Path, settings: dict, user_id: int,
                             progress_cb: ProgressCB, cancel_event: asyncio.Event | None, reason: str) -> list[Path]:
    """The video is already downloaded (without subtitles). Fetch the subtitles separately and apply what the
    person chose; if they still can't be had, say why."""
    progress_cb(None, None, None, "Fetching subtitles")
    fetched = await _fetch_subtitles(url, workspace, settings, user_id, cancel_event)
    if not fetched:
        _add_note(settings, _subtitle_failure_note(reason))
        return files
    files = await _apply_subtitles(files, fetched, workspace, settings, progress_cb, cancel_event)
    wanted = list(settings["sub_langs"])
    if len(fetched) < len(wanted):
        _add_note(settings, _subtitle_failure_note(
            reason, partial=f"Only {len(fetched)} of {len(wanted)} subtitle languages could be fetched"))
    return files


async def _apply_subtitles(files: list[Path], fetched: dict[str, Path], workspace: Path, settings: dict,
                           progress_cb: ProgressCB, cancel_event: asyncio.Event | None) -> list[Path]:
    """Deliver separately fetched subtitles the way the person chose: embedded track, .srt files, burned in."""
    mode = settings.get("sub_mode", "embed")
    video = next((f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")), None)
    others = [f for f in files if f is not video]
    stem = video.stem if video else "subtitles"
    srts: list[tuple[str, Path]] = []
    for lang in settings["sub_langs"]:                     # in the order the person picked them
        path = fetched.get(lang)
        if path is None:
            continue
        dest = workspace / f"{stem}.{lang}.srt"
        if path != dest:
            path.replace(dest)
        srts.append((lang, dest))
    srt_files = [path for _, path in srts]
    if video is None:
        return others + srt_files

    if mode in ("embed", "both"):
        try:
            embedded = await tools.embed_subtitles(video, srts, workspace, await _media_seconds(video), progress_cb,
                                                   cancel_event)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Embedding the fetched subtitles failed: %s", exc)
            _add_note(settings, "◧ Couldn't embed the subtitles, so they were sent as separate .srt files.")
            return [video] + others + srt_files
        final = video.with_suffix(".mp4")
        video.unlink(missing_ok=True)
        embedded.replace(final)
        return [final] + others + (srt_files if mode == "both" else [])
    if mode in ("burn", "burnfile"):
        return await _burn_subtitles([video] + others + srt_files, workspace, settings, progress_cb, cancel_event)
    return [video] + others + srt_files                      # "file"


def _srt_language(path: Path) -> str:
    parts = path.suffixes
    return parts[-2].lstrip(".") if len(parts) >= 2 else ""


async def _burn_subtitles(files: list[Path], workspace: Path, settings: dict, progress_cb: ProgressCB,
                          cancel_event: asyncio.Event | None) -> list[Path]:
    """"Burned in": draw the FIRST chosen language into the picture (re-encodes the video). The other languages
    stay as .srt files. If it can't be done (too long, ffmpeg refuses) the video goes out unchanged with its
    .srt files and the person is told - the download itself is never lost."""
    video = next((f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")), None)
    srts = [f for f in files if f.suffix.lower() == ".srt"]
    if video is None or not srts:
        return files                                      # nothing to burn; _verify_subtitles already said why
    by_language = {_srt_language(f): f for f in srts}
    chosen = next((by_language[lang] for lang in settings.get("sub_langs") or [] if lang in by_language), srts[0])
    duration = await _media_seconds(video)
    if duration and duration > config.BURN_MAX_SECONDS:
        _add_note(settings, f"◧ This video is too long to burn subtitles into ({int(duration // 60)} min), so they "
                            f"were sent as a separate .srt file instead.")
        return files
    try:
        burned = await tools.burn_into(video, chosen, workspace, duration, progress_cb, cancel_event, stem=video.stem)
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Burning subtitles failed: %s", exc)
        _add_note(settings, "◧ Couldn't burn the subtitles into the video, so they were sent as a separate .srt file.")
        return files
    final = video.with_suffix(".mp4")
    video.unlink(missing_ok=True)
    burned.replace(final)
    keep_chosen = settings.get("sub_mode") == "burnfile"        # ".srt file" is also on: every language comes as a file
    if not keep_chosen:
        chosen.unlink(missing_ok=True)
    if len(srts) > 1:
        _add_note(settings, "◧ Only the first language is burned in; the others are attached as .srt files.")
    return [final] + [f for f in files if f is not video and (keep_chosen or f is not chosen)]


async def _subtitle_streams(path: Path) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return len([line for line in out.decode().splitlines() if line.strip()])
    except OSError:
        return -1                                     # can't tell: don't claim anything is missing


async def _verify_subtitles(files: list[Path], settings: dict) -> list[Path]:
    """yt-dlp only WARNS when a subtitle track can't be fetched (YouTube rate-limits them),
    so the video arrives without subtitles and nobody knows. Check, and say so."""
    wanted = list(settings["sub_langs"])
    mode = settings.get("sub_mode", "embed")
    video = next((f for f in files if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")), None)
    srt_count = sum(1 for f in files if f.suffix.lower() == ".srt")
    embedded = await _subtitle_streams(video) if video and mode in ("embed", "both") else None

    got = {"embed": embedded, "file": srt_count, "both": max(embedded or 0, srt_count), "burn": srt_count,
           "burnfile": srt_count}[mode]
    if got == -1:
        return files
    if got == 0:
        _add_note(settings, "◧ No subtitles could be fetched (YouTube may be limiting subtitle requests). "
                            "The video was sent without them.")
    elif got < len(wanted):
        _add_note(settings, f"◧ Only {got} of {len(wanted)} subtitle languages could be fetched.")
    return files


async def _download_main(url: str, workspace: Path, settings: dict, user_id: int,
                         progress_cb: ProgressCB, cancel_event: asyncio.Event | None = None) -> list[Path]:
    """Runs yt-dlp in a worker thread (it's blocking) and reports progress
    back onto the asyncio event loop via progress_cb.

    On a "sign in to confirm you're not a bot" error, retries with
    alternate player clients before giving up - see CLIENT_FALLBACKS.
    Skipped when the user has cookies enabled, since mixing cookies with
    some clients (notably tv) can invalidate the cookie session.

    After the raw download finishes, yt-dlp's own progress hook goes
    silent while postprocessors (merging, embedding thumbnails/metadata)
    run - which used to make the UI look frozen at ~98-100% for however
    long that takes. postprocessor_hooks fills that gap with a live
    stage label instead.

    cancel_event: yt-dlp runs in a worker thread, so asyncio.Task.cancel()
    alone can't touch it - the task just gets cancelled once yt-dlp
    eventually returns control, letting the download run to completion in
    the background regardless. Checking this event from inside the hooks
    (which yt-dlp calls from that same thread) and raising
    DownloadCancelled is yt-dlp's own documented way to abort a download
    that's actually in progress."""
    if settings.get("sections"):
        return await _download_sections(url, workspace, settings, user_id, progress_cb, cancel_event)

    loop = asyncio.get_running_loop()
    stream_sizes: dict[str, int] = {}   # per-stream totals seen so far -> running overall size
    seen_info: dict = {}                # the video's info (title, artist, chapters...) as the hooks saw it

    def _check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")

    # progress_cb positional args: (percent, speed, eta, stage, label, size)
    # - positional because loop.call_soon_threadsafe can't pass kwargs.
    adhd = bool(settings.get("adhd_mode"))
    stream_peak: dict[str, float] = {}     # highest % shown per stream: a bar never moves backwards
    stream_bytes: dict[str, int] = {}      # ADHD: bytes fetched so far per stream
    overall_peak = 0.0
    streams_finished = 0

    def _expected(info: dict) -> tuple[int | None, int]:
        """(total bytes across every stream of this download, how many streams),
        when the site announced the sizes up front - YouTube does."""
        formats = info.get("requested_formats") or [info]
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
        return (sum(sizes) if all(sizes) else None), len(formats)

    def hook(d: dict) -> None:
        nonlocal overall_peak, streams_finished
        _check_cancelled()
        status = d.get("status")
        if status not in ("downloading", "finished"):
            return
        info = d.get("info_dict") or {}
        if info:
            seen_info["info"] = info
        label = _stream_label(info, settings)
        key = str(info.get("format_id") or d.get("filename") or label)
        # ADHD Mode shows one bar and no stream labels, so two 0-100% runs
        # (video, then audio) looked like "finished, then downloading again".
        # Count both streams toward ONE overall bar instead, when sizes are known.
        expected, stream_count = _expected(info) if adhd else (None, 0)

        if status == "finished":
            # Fires once PER STREAM (video, then audio). Marks that
            # stream's line complete; the next stream's first tick starts
            # a new line (job_manager notices the label change), so the
            # finished line stays behind as "Video - 100%".
            streams_finished += 1
            if expected:
                stream_bytes[key] = max(stream_bytes.get(key, 0), d.get("total_bytes") or d.get("downloaded_bytes") or 0)
                overall = 100.0 if streams_finished >= stream_count else min(99.0, sum(stream_bytes.values()) / expected * 100)
                overall_peak = max(overall_peak, overall)
                loop.call_soon_threadsafe(progress_cb, overall_peak, None, None, None, label)
            else:
                loop.call_soon_threadsafe(progress_cb, 100.0, None, None, None, label)
            return

        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        percent = (downloaded / total * 100) if total else 0.0
        # Size estimates change as a download goes on and some ticks carry no
        # total at all (-> 0%): never let the bar step backwards because of it.
        percent = max(percent, stream_peak.get(key, 0.0))
        stream_peak[key] = percent
        if expected:
            stream_bytes[key] = downloaded
            overall_peak = max(overall_peak, min(99.0, sum(stream_bytes.values()) / expected * 100))
            percent = overall_peak
        speed = _fmt_speed(d.get("speed"))
        eta = d.get("_eta_str", "").strip() or None

        if total and key not in stream_sizes:
            stream_sizes[key] = total
            # overall size = every stream seen so far (video, then + audio)
            loop.call_soon_threadsafe(progress_cb, None, None, None, None, None, sum(stream_sizes.values()))
        loop.call_soon_threadsafe(progress_cb, percent, speed, eta, None, label)

    def pp_hook(d: dict) -> None:
        _check_cancelled()
        if d.get("info_dict"):
            seen_info["info"] = d["info_dict"]
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            loop.call_soon_threadsafe(progress_cb, 100.0, None, None, _postprocessor_label(name))

    base_opts = _build_opts(url, workspace, settings, user_id, hook, pp_hook)
    using_cookies = "cookiefile" in base_opts

    def run(opts: dict) -> list[Path]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = sorted(workspace.glob("*"))
        if settings.get("mode") == "audio" and settings.get("audio_format") == "opus":
            files = [_rename_opus_ogg(f) for f in files]
        return files

    def clear_partial_output() -> None:
        for leftover in workspace.iterdir():
            try:
                if leftover.is_file():
                    leftover.unlink()
            except OSError:
                pass

    async def reap_when_cancelled() -> None:
        # yt-dlp only notices a cancel at its next progress callback, which never comes while ffmpeg
        # is merging/converting - so stop that ffmpeg (repeatedly: yt-dlp may start another) ourselves.
        while True:
            await asyncio.sleep(1)
            if cancel_event is not None and cancel_event.is_set():
                kill_processes_under(workspace)

    async def run_with_timeout(opts: dict) -> list[Path]:
        reaper = asyncio.create_task(reap_when_cancelled())
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, run, opts), timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            kill_processes_under(workspace)      # the thread can't be stopped, but its ffmpeg can
            raise RuntimeError(
                f"Timed out after {DOWNLOAD_TIMEOUT_SECONDS // 60} minutes - "
                "something (likely a postprocessing step) got stuck."
            )
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception:
            # Killing ffmpeg makes yt-dlp fail with a generic error: if that is why we are here, it's a cancel.
            _raise_if_cancelled(cancel_event)
            raise
        finally:
            reaper.cancel()

    try:
        results = await run_with_timeout(base_opts)
    except yt_dlp.utils.DownloadCancelled as exc:
        raise JobCancelled(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if using_cookies or not _looks_like_bot_check(exc):
            raise
        last_error: Exception = exc
        for client in CLIENT_FALLBACKS:
            _check_cancelled()
            clear_partial_output()
            retry_opts = dict(base_opts)
            retry_opts["extractor_args"] = {
                **base_opts.get("extractor_args", {}),
                "youtube": {"player_client": [client]},
            }
            try:
                log.info("Retrying %s with player_client=%s after bot-check", url, client)
                results = await run_with_timeout(retry_opts)
                last_error = None  # type: ignore[assignment]
                break
            except yt_dlp.utils.DownloadCancelled as retry_exc:
                raise JobCancelled(str(retry_exc)) from retry_exc
            except Exception as retry_exc:  # noqa: BLE001
                last_error = retry_exc
                continue
        if last_error is not None:
            raise last_error

    # filter out leftover thumbnail/metadata sidecar files, keep final media
    media = [p for p in results if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}]
    files = media or results
    try:
        chosen = (seen_info.get("info") or {}).get("requested_formats") or [seen_info.get("info") or {}]
        log.info("Size check: formats %s, announced %s, actually fetched %s bytes",
                 "+".join(str(f.get("format_id")) for f in chosen),
                 [f.get("filesize") or f.get("filesize_approx") for f in chosen], sum(stream_sizes.values()))
    except Exception:  # noqa: BLE001
        pass
    if settings.get("mode") == "audio":
        files = await _finish_audio(files, seen_info.get("info") or {}, settings, progress_cb, cancel_event)
    return files


async def _finish_audio(files: list[Path], info: dict, settings: dict, progress_cb: ProgressCB,
                        cancel_event: asyncio.Event | None) -> list[Path]:
    """The music finishing touches (see downloader/music.py): proper tags and a
    square cover on an mp3, and - when asked for - one track per chapter. Only
    for a single audio file (a playlist is left exactly as downloaded), and
    never at the cost of the download itself."""
    audio_format = settings.get("audio_format")
    candidates = [f for f in files if f.suffix.lower() == f".{audio_format}"]
    if len(candidates) != 1 or audio_format not in ("mp3", "opus"):
        return files
    path = candidates[0]

    try:
        tags = music_tags(info)
        if audio_format == "mp3":
            await polish_mp3(path, tags, cancel_event)

        if settings.get("split_chapters"):
            chapters = valid_chapters(info)
            if 2 <= len(chapters) <= MAX_TRACKS:
                progress_cb(None, None, None, f"Splitting into {len(chapters)} tracks")
                shared = {k: v for k, v in tags.items() if k != "title"}
                shared.setdefault("album", clean_title(info.get("title") or ""))      # the mix is the album
                tracks = await split_by_chapters(path, chapters, {k: v for k, v in shared.items() if v}, cancel_event)
                if tracks:
                    path.unlink(missing_ok=True)
                    return tracks
                progress_cb(None, None, None, "Couldn't split - sending the whole file")
            else:
                progress_cb(None, None, None, "No chapters to split by - sending the whole file")
    except JobCancelled:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Music finishing failed for %s; using the file as downloaded", path.name)
    return files


# ---------------------------------------------------------------------------
# Time-range clips ("sections")
#
# Measured on a real 1h43m YouTube video (spike, 2026-09): a 2-minute clip
# transferred ~18 MB instead of ~800 MB and came out exactly 120.0s, so yt-dlp's
# download_ranges genuinely fetches only the range. What the spike ALSO showed
# shapes this code:
#   - Several ranges in ONE yt-dlp call silently yield a single file (the
#     second range is lost) -> we run yt-dlp once PER section.
#   - yt-dlp fires exactly one progress event, "finished", at the very end
#     (ffmpeg does the transfer, not yt-dlp) -> no live percent bar is
#     possible; we report "Clip 1 of 2 - 1:05" instead.
#   - The hooks therefore never run mid-download, so the usual cancel-by-
#     raising-in-a-hook can't interrupt a clip -> we kill the job's ffmpeg.
# ---------------------------------------------------------------------------

_NON_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}
_WATCH_INTERVAL = 1.0          # seconds between looks at a running clip download
_ELAPSED_EVERY_TICKS = 4       # elapsed-time line cadence while no real progress is available
_NO_PROGRESS_WARN_TICKS = 25   # then log once why the bar never appeared


def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("Cancelled by user")


def _kill_ffmpeg_under(workspace: Path) -> int:
    """Kept under this name for the clip code; see utils/procs.py."""
    return kill_processes_under(workspace)


def _tidy_clip_name(path: Path) -> Path:
    """Titles are cut by yt-dlp's %(title).60B, which can end on a space and
    leave 'Some title  [01-30-00-01-32-00].mp4'. Collapse that."""
    cleaned = re.sub(r"\s+", " ", path.stem).strip()
    if cleaned == path.stem:
        return path
    target = path.with_name(cleaned + path.suffix)
    if target.exists():
        return path
    try:
        path.rename(target)
        return target
    except OSError:
        return path


def _unique_destination(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    counter = 2
    while target.exists():
        target = dest_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
        counter += 1
    return target


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class _FfmpegProgress:
    """Follows ffmpeg's own `-progress <file>` output for one clip.

    yt-dlp reports nothing while ffmpeg transfers a range (spike: a single
    "finished" event at the very end), but ffmpeg itself can log how far it
    is: blocks of key=value lines, each closed by progress=continue|end, with
    out_time_us = how much OUTPUT it has produced so far. For a clip that is
    directly "seconds done of clip length".

    yt-dlp runs one ffmpeg per format (video, then audio), each reopening the
    same file from scratch, so output time going backwards (or continuing
    after an "end") means the next stream has started."""

    def __init__(self, path: Path, clip_seconds: float):
        self.path = path
        self.total_us = max(clip_seconds, 1.0) * 1_000_000
        self.stream = 0
        self._last_us = -1
        self._ended = False

    def poll(self) -> tuple[int, float] | None:
        """(stream index, percent), or None while there's nothing usable yet."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 4096))
                text = fh.read().decode("utf-8", "ignore")
        except OSError:
            return None
        # Only COMPLETE blocks count: we may read while ffmpeg is mid-write.
        complete = list(re.finditer(r"^progress=\w*\n", text, re.M))
        if not complete:
            return None

        values: dict[str, str] = {}
        newest: dict[str, str] | None = None
        status = ""
        for line in text[:complete[-1].end()].splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "progress":
                newest, status, values = values, value.strip(), {}
            else:
                values[key.strip()] = value.strip()
        if newest is None:
            return None

        raw = newest.get("out_time_us") or newest.get("out_time_ms")   # (_ms is really microseconds too)
        try:
            out_us = int(raw)
        except (TypeError, ValueError):
            out_us = None
        ended = status == "end"
        if out_us is None or out_us < 0:
            if not ended:
                return None                  # "N/A" before the first frame
            out_us = int(self.total_us)

        if out_us < self._last_us - 1_000_000 or (self._ended and not ended):
            self.stream += 1
        self._last_us, self._ended = out_us, ended
        return self.stream, 100.0 if ended else min(99.0, out_us / self.total_us * 100)


def _ffmpeg_flags_under(workspace: Path) -> list[str]:
    """Cut/progress flags of this job's running ffmpegs (never the signed input
    URLs) - logged when the progress bar doesn't show up, so the cause is visible."""
    wanted = {"-ss", "-t", "-to", "-progress", "-c", "-c:v", "-c:a"}
    needle = (str(workspace).rstrip("/") + "/").encode()
    found = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
            if not argv or b"ffmpeg" not in os.path.basename(argv[0]) or needle not in b" ".join(argv):
                continue
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in argv]
        found.append(" ".join(f"{a} {args[i + 1]}" for i, a in enumerate(args[:-1]) if a in wanted) or "(no flags)")
    return found


async def _watch_clip(workspace: Path, cancel_event: asyncio.Event | None, progress_cb: ProgressCB,
                      text: str, started: float, progress: _FfmpegProgress | None = None,
                      labels: tuple[str, ...] = ("Video", "Audio")) -> None:
    """Runs alongside a clip download: (1) enforces cancellation by killing
    ffmpeg, (2) reports progress - a real bar from ffmpeg's -progress output
    when it is available, otherwise elapsed time (yt-dlp itself reports
    nothing, so without this the message would look frozen).

    Elapsed time also covers the quiet start (extraction, PO token) before
    ffmpeg produces anything; it stops the moment real progress arrives.
    The elapsed text travels in progress_cb's `speed` slot with everything
    else None - job_manager renders that as a plain overwriting line."""
    ticks = 0
    have_bar = False
    warned = False
    while True:
        await asyncio.sleep(_WATCH_INTERVAL)
        if cancel_event is not None and cancel_event.is_set():
            # Repeatedly: yt-dlp may start another ffmpeg (thumbnail, remux)
            # before it notices the first one died.
            _kill_ffmpeg_under(workspace)
            continue
        ticks += 1

        reading = progress.poll() if progress is not None else None
        if reading is not None:
            if not have_bar:
                have_bar = True
                log.info("Clip progress is coming from ffmpeg -progress (%s)", text)
            stream, percent = reading
            progress_cb(percent, None, None, None, labels[min(stream, len(labels) - 1)])
            continue

        if not have_bar and ticks % _ELAPSED_EVERY_TICKS == 0:
            progress_cb(None, f"{text} · {_fmt_elapsed(time.monotonic() - started)}", None)
        if not have_bar and not warned and ticks >= _NO_PROGRESS_WARN_TICKS:
            warned = True
            log.info("No ffmpeg progress data after %s ticks; showing elapsed time instead. "
                     "Running ffmpeg flags: %s", ticks, _ffmpeg_flags_under(workspace) or "none running")


async def _run_ffmpeg(args: list[str], cancel_event: asyncio.Event | None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    waiter = asyncio.create_task(proc.communicate())
    started = time.monotonic()
    while not waiter.done():
        await asyncio.wait({waiter}, timeout=0.5)
        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled or time.monotonic() - started > DOWNLOAD_TIMEOUT_SECONDS:
            try:
                proc.kill()
            except ProcessLookupError:
                pass                        # it finished in the instant between the check and the kill
            await waiter
            if cancelled:
                raise JobCancelled("Cancelled by user")
            raise RuntimeError("Merging the clips took too long and was stopped.")
    _, err = waiter.result()
    return proc.returncode or 0, err.decode("utf-8", "replace")[-300:]


async def _media_seconds(path: Path) -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip())
    except (OSError, ValueError):
        return None


async def _merge_clips(clips: list[Path], sections: list[tuple[float, float]], workspace: Path,
                       cancel_event: asyncio.Event | None) -> Path | None:
    """Join clips into one file, in order. Returns None if it can't (callers
    then send the clips separately - better than failing after all the work).

    First tries a lossless concat (fast, no quality loss). The clips all come
    from one source with one format selector, so that normally works, but
    concat-copy can produce timestamp glitches, so the result's length is
    checked and only a wrong length triggers the slower re-encode."""
    if len({c.suffix.lower() for c in clips}) != 1:
        return None
    suffix = clips[0].suffix
    base = re.sub(r"\s*\[[^\]]*\]$", "", clips[0].stem).strip() or "clips"
    out = _unique_destination(workspace, f"{base} [{len(clips)} clips]{suffix}")
    listing = workspace / "concat_list.txt"
    listing.write_text("".join("file '{}'\n".format(str(c).replace("'", "'\\''")) for c in clips), encoding="utf-8")
    expected = sum(end - start for start, end in sections)

    try:
        for reencode in (False, True):
            _raise_if_cancelled(cancel_event)
            args = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
            args += [] if reencode else ["-c", "copy"]
            args.append(str(out))
            code, err = await _run_ffmpeg(args, cancel_event)
            if code == 0 and out.exists() and out.stat().st_size > 0:
                got = await _media_seconds(out)
                if got is not None and abs(got - expected) <= max(3.0, expected * 0.02):
                    return out
                log.info("Concat (reencode=%s) length %s != expected %s", reencode, got, expected)
            else:
                log.info("Concat (reencode=%s) failed: %s", reencode, err.strip())
            out.unlink(missing_ok=True)
        return None
    finally:
        listing.unlink(missing_ok=True)


async def _download_sections(url: str, workspace: Path, settings: dict, user_id: int,
                             progress_cb: ProgressCB, cancel_event: asyncio.Event | None) -> list[Path]:
    """Download only the requested time ranges of one video or audio track.

    settings["sections"]: [(start_s, end_s), ...], already concrete (see
    downloader.sections). settings["sections_merge"]: join into one file.
    Any failed clip fails the whole job with a message naming the clip - a
    silent partial result would be worse than a clear error and a retry."""
    from yt_dlp.utils import download_range_func   # local: keeps module import cheap for tests

    loop = asyncio.get_running_loop()
    try:
        sections = validate_sections(settings["sections"])
    except SectionError as exc:
        raise RuntimeError(str(exc)) from exc
    total = len(sections)
    merge = bool(settings.get("sections_merge")) and total > 1
    # Stream order yt-dlp downloads in: video first, then audio (audio jobs: just audio).
    labels = ("Audio",) if settings.get("mode") == "audio" else ("Video", "Audio")

    def _check_cancelled_hook(_d: dict) -> None:
        # Only fires around the download (never mid-transfer, see above), but
        # it is free and stops the postprocessing phase promptly.
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")

    clips: list[Path] = []
    for index, (start, end) in enumerate(sections, 1):
        _raise_if_cancelled(cancel_event)
        text = f"Clip {index} of {total} · {format_section(start, end)}" if total > 1 \
            else f"Clip · {format_section(start, end)}"
        progress_cb(None, None, None, text)          # stage -> a new status line

        clip_dir = workspace / f"clip{index:02d}"
        clip_dir.mkdir(exist_ok=True)
        clip_settings = dict(settings)
        clip_settings.pop("sub_langs", None)        # subtitles are downloaded for the WHOLE video: out of sync on a clip
        clip_settings["filename_template"] = f"%(title).60B [{filename_tag(start, end)}].%(ext)s"
        base_opts = _build_opts(url, clip_dir, clip_settings, user_id, _check_cancelled_hook, _check_cancelled_hook)
        # Cut exactly where asked (re-encodes just this small piece) instead
        # of snapping to the nearest keyframe, which can be seconds off.
        base_opts["download_ranges"] = download_range_func(None, [(start, end)])
        base_opts["force_keyframes_at_cuts"] = True
        # Ask ffmpeg to log its own progress to a file (see _FfmpegProgress).
        # "ffmpeg_i" = yt-dlp's per-input ffmpeg args; -progress is a global
        # option, so it is valid in that position. If this key were ever not
        # honoured the watcher just falls back to elapsed time (and logs why).
        progress_file = workspace / f"ffmpeg_progress_{index}.txt"
        ff_args = dict(base_opts.get("external_downloader_args") or {})
        ff_args["ffmpeg_i"] = [*ff_args.get("ffmpeg_i", []), "-progress", str(progress_file)]
        base_opts["external_downloader_args"] = ff_args
        using_cookies = "cookiefile" in base_opts

        def run(opts: dict) -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        async def attempt(opts: dict, _text: str = text, _file: Path = progress_file,
                          _length: float = end - start) -> None:
            _file.unlink(missing_ok=True)       # a retry must not read the failed run's numbers
            watcher = asyncio.create_task(_watch_clip(
                workspace, cancel_event, progress_cb, _text, time.monotonic(),
                _FfmpegProgress(_file, _length), labels))
            try:
                await asyncio.wait_for(loop.run_in_executor(None, run, opts), timeout=DOWNLOAD_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                _kill_ffmpeg_under(workspace)
                raise RuntimeError(f"Timed out after {DOWNLOAD_TIMEOUT_SECONDS // 60} minutes.")
            except yt_dlp.utils.DownloadCancelled as exc:
                raise JobCancelled(str(exc)) from exc
            except Exception:
                # Killing ffmpeg makes yt-dlp fail with a generic error;
                # if that's why we're here, it's a cancel, not a failure.
                _raise_if_cancelled(cancel_event)
                raise
            finally:
                watcher.cancel()

        try:
            await attempt(base_opts)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if using_cookies or not _looks_like_bot_check(exc):
                raise RuntimeError(f"Clip {index} failed: {str(exc).strip().splitlines()[0][:160]}") from exc
            last_error: Exception | None = exc
            for client in CLIENT_FALLBACKS:
                _raise_if_cancelled(cancel_event)
                for leftover in clip_dir.iterdir():
                    leftover.unlink(missing_ok=True)
                retry_opts = dict(base_opts)
                retry_opts["extractor_args"] = {
                    **base_opts.get("extractor_args", {}),
                    "youtube": {"player_client": [client]},
                }
                try:
                    log.info("Retrying clip %s of %s with player_client=%s after bot-check", index, url, client)
                    await attempt(retry_opts)
                    last_error = None
                    break
                except JobCancelled:
                    raise
                except Exception as retry_exc:  # noqa: BLE001
                    last_error = retry_exc
            if last_error is not None:
                raise RuntimeError(f"Clip {index} failed: {str(last_error).strip().splitlines()[0][:160]}") from last_error

        produced = [f for f in sorted(clip_dir.glob("*")) if f.suffix.lower() not in _NON_MEDIA_SUFFIXES]
        if settings.get("mode") == "audio" and settings.get("audio_format") == "opus":
            produced = [_rename_opus_ogg(f) for f in produced]
        produced = [f for f in produced if f.exists() and f.stat().st_size > 0]
        if not produced:
            raise RuntimeError(f"Clip {index} came out empty.")
        # Flatten into the workspace root, where job_manager expects results.
        for f in produced:
            clips.append(_tidy_clip_name(Path(shutil.move(str(f), str(_unique_destination(workspace, f.name))))))
        shutil.rmtree(clip_dir, ignore_errors=True)
        progress_file.unlink(missing_ok=True)

    if merge:
        progress_cb(None, None, None, "Merging clips")
        merged = await _merge_clips(clips, sections, workspace, cancel_event)
        if merged is not None:
            for c in clips:
                c.unlink(missing_ok=True)
            return [merged]
        progress_cb(None, None, None, "Couldn't merge - sending the clips separately")
    return clips
