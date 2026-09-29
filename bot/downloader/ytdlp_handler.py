import asyncio
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Callable

import yt_dlp

from config import DATA_DIR, BGUTIL_POT_URL
from downloader.cookies import apply_cookies, cookie_file_for
from downloader.errors import JobCancelled
from downloader.sections import SectionError, filename_tag, format_section, validate_sections

log = logging.getLogger("candy.ytdlp")

# percent, speed, eta, stage-label-override (used during post-processing,
# when yt-dlp's own download-progress hook goes silent for a while)
ProgressCB = Callable[[float, str | None, str | None, str | None], None]

# A genuinely hung ffmpeg/postprocessor step (rare, but possible) would
# otherwise freeze the UI forever with no error - bound it instead.
DOWNLOAD_TIMEOUT_SECONDS = 20 * 60

_POSTPROCESSOR_LABELS = {
    "Merger": "Merging video & audio",
    "FFmpegVideoConvertor": "Converting video",
    "FFmpegExtractAudio": "Extracting audio",
    "EmbedThumbnail": "Embedding thumbnail",
    "FFmpegMetadata": "Adding metadata",
    "FFmpegEmbedSubtitle": "Embedding subtitles",
    "SponsorBlock": "Checking for sponsor segments",
    "ModifyChapters": "Removing sponsor segments",
}


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


async def download(url: str, workspace: Path, settings: dict, user_id: int,
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

    def _check_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")

    # progress_cb positional args: (percent, speed, eta, stage, label, size)
    # - positional because loop.call_soon_threadsafe can't pass kwargs.
    def hook(d: dict) -> None:
        _check_cancelled()
        status = d.get("status")
        if status not in ("downloading", "finished"):
            return
        info = d.get("info_dict") or {}
        label = _stream_label(info, settings)

        if status == "finished":
            # Fires once PER STREAM (video, then audio). Marks that
            # stream's line complete; the next stream's first tick starts
            # a new line (job_manager notices the label change), so the
            # finished line stays behind as "Video - 100%".
            loop.call_soon_threadsafe(progress_cb, 100.0, None, None, None, label)
            return

        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes", 0)
        percent = (downloaded / total * 100) if total else 0.0
        speed = _fmt_speed(d.get("speed"))
        eta = d.get("_eta_str", "").strip() or None

        key = str(info.get("format_id") or d.get("filename") or label)
        if total and key not in stream_sizes:
            stream_sizes[key] = total
            # overall size = every stream seen so far (video, then + audio)
            loop.call_soon_threadsafe(progress_cb, None, None, None, None, None, sum(stream_sizes.values()))
        loop.call_soon_threadsafe(progress_cb, percent, speed, eta, None, label)

    def pp_hook(d: dict) -> None:
        _check_cancelled()
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            label = _POSTPROCESSOR_LABELS.get(name, name or "Finishing up...")
            loop.call_soon_threadsafe(progress_cb, 100.0, None, None, label)

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

    async def run_with_timeout(opts: dict) -> list[Path]:
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, run, opts), timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Timed out after {DOWNLOAD_TIMEOUT_SECONDS // 60} minutes - "
                "something (likely a postprocessing step) got stuck."
            )

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
    return media or results


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
_ELAPSED_TICK_SECONDS = 4


def _raise_if_cancelled(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled("Cancelled by user")


def _kill_ffmpeg_under(workspace: Path) -> int:
    """SIGKILL every ffmpeg whose command line mentions this job's workspace.

    That is how a clip download is cancelled: the transfer runs inside an
    ffmpeg child of yt-dlp that no hook can reach. The workspace path is
    unique per job (see utils.cleanup.job_workspace), so other jobs' ffmpegs
    are never touched. Reads /proc, so it is a harmless no-op off Linux."""
    needle = str(workspace).rstrip("/") + "/"
    killed = 0
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
            if not argv or b"ffmpeg" not in os.path.basename(argv[0]):
                continue
            if needle.encode() not in b" ".join(argv):
                continue
            os.kill(int(cmdline.parent.name), signal.SIGKILL)
            killed += 1
        except (OSError, ValueError):
            continue   # process vanished, or not ours to kill
    return killed


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


async def _watch_clip(workspace: Path, cancel_event: asyncio.Event | None,
                      progress_cb: ProgressCB, text: str, started: float) -> None:
    """Runs alongside a clip download: (1) enforces cancellation by killing
    ffmpeg, (2) keeps the status line alive with elapsed time, since yt-dlp
    reports nothing while ffmpeg works and the message would look frozen.

    The elapsed text travels in progress_cb's `speed` slot with everything
    else None - job_manager renders that as a plain overwriting line."""
    ticks = 0
    while True:
        await asyncio.sleep(1)
        if cancel_event is not None and cancel_event.is_set():
            # Repeatedly: yt-dlp may start another ffmpeg (thumbnail, remux)
            # before it notices the first one died.
            _kill_ffmpeg_under(workspace)
            continue
        ticks += 1
        if ticks % _ELAPSED_TICK_SECONDS == 0:
            progress_cb(None, f"{text} · {_fmt_elapsed(time.monotonic() - started)}", None)


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
            proc.kill()
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
        clip_settings["filename_template"] = f"%(title).60B [{filename_tag(start, end)}].%(ext)s"
        base_opts = _build_opts(url, clip_dir, clip_settings, user_id, _check_cancelled_hook, _check_cancelled_hook)
        # Cut exactly where asked (re-encodes just this small piece) instead
        # of snapping to the nearest keyframe, which can be seconds off.
        base_opts["download_ranges"] = download_range_func(None, [(start, end)])
        base_opts["force_keyframes_at_cuts"] = True
        using_cookies = "cookiefile" in base_opts

        def run(opts: dict) -> None:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        async def attempt(opts: dict, _dir: Path = clip_dir, _text: str = text) -> None:
            watcher = asyncio.create_task(_watch_clip(workspace, cancel_event, progress_cb, _text, time.monotonic()))
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

    if merge:
        progress_cb(None, None, None, "Merging clips")
        merged = await _merge_clips(clips, sections, workspace, cancel_event)
        if merged is not None:
            for c in clips:
                c.unlink(missing_ok=True)
            return [merged]
        progress_cb(None, None, None, "Couldn't merge - sending the clips separately")
    return clips
