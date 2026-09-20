import asyncio
import logging
from pathlib import Path
from typing import Callable

import yt_dlp

from config import COOKIES_DIR, DATA_DIR, BGUTIL_POT_URL

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


def _format_selector(s: dict) -> str:
    if s["mode"] == "audio":
        return "bestaudio/best"

    quality = s["quality"]
    if quality == "best":
        base = "bestvideo+bestaudio/best"
    elif quality == "worst":
        base = "worstvideo+worstaudio/worst"
    else:
        height = quality.rstrip("p")
        base = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    return base


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

    if s["cookies_enabled"]:
        cookie_file = Path(COOKIES_DIR) / f"{user_id}.txt"
        if cookie_file.exists():
            opts["cookiefile"] = str(cookie_file)

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
                    progress_cb: ProgressCB) -> list[Path]:
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
    stage label instead."""
    loop = asyncio.get_running_loop()

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0.0
            speed = d.get("_speed_str", "").strip() or None
            eta = d.get("_eta_str", "").strip() or None
            loop.call_soon_threadsafe(progress_cb, percent, speed, eta, None)
        elif d.get("status") == "finished":
            loop.call_soon_threadsafe(progress_cb, 100.0, None, None, "Finishing up...")

    def pp_hook(d: dict) -> None:
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            label = _POSTPROCESSOR_LABELS.get(name, name or "Finishing up...")
            loop.call_soon_threadsafe(progress_cb, 100.0, None, None, label)

    base_opts = _build_opts(url, workspace, settings, user_id, hook, pp_hook)
    using_cookies = "cookiefile" in base_opts

    def run(opts: dict) -> list[Path]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return sorted(workspace.glob("*"))

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
    except Exception as exc:  # noqa: BLE001
        if using_cookies or not _looks_like_bot_check(exc):
            raise
        last_error: Exception = exc
        for client in CLIENT_FALLBACKS:
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
            except Exception as retry_exc:  # noqa: BLE001
                last_error = retry_exc
                continue
        if last_error is not None:
            raise last_error

    # filter out leftover thumbnail/metadata sidecar files, keep final media
    media = [p for p in results if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}]
    return media or results

