import asyncio
import logging
from pathlib import Path
from typing import Callable

import yt_dlp

from config import COOKIES_DIR, DATA_DIR, BGUTIL_POT_URL

log = logging.getLogger("candy.ytdlp")

ProgressCB = Callable[[float, str | None, str | None], None]  # percent, speed, eta


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


def _build_opts(url: str, workspace: Path, s: dict, user_id: int, progress_hook) -> dict:
    outtmpl = str(workspace / s["filename_template"])

    opts: dict = {
        "outtmpl": outtmpl,
        "format": _format_selector(s),
        "noplaylist": s["playlist_mode"] == "single",
        "progress_hooks": [progress_hook],
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
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": s["audio_format"],
            "preferredquality": str(s["audio_bitrate"]),
        })
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
    some clients (notably tv) can invalidate the cookie session."""
    loop = asyncio.get_running_loop()

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0.0
            speed = d.get("_speed_str", "").strip() or None
            eta = d.get("_eta_str", "").strip() or None
            loop.call_soon_threadsafe(progress_cb, percent, speed, eta)
        elif d.get("status") == "finished":
            loop.call_soon_threadsafe(progress_cb, 100.0, None, None)

    base_opts = _build_opts(url, workspace, settings, user_id, hook)
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

    try:
        results = await loop.run_in_executor(None, run, base_opts)
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
                results = await loop.run_in_executor(None, run, retry_opts)
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

