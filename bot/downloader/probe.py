"""
Before showing quality buttons, actually ask yt-dlp what's available for
this specific link, instead of showing a fixed list that may not match
reality (e.g. offering 1080p on a video that tops out at 720p, or
offering quality choices at all on a site that has none).
"""
import asyncio
import logging
from dataclasses import dataclass, field

import yt_dlp

from config import BGUTIL_POT_URL
from downloader.cookies import apply_cookies, cookie_file_for
from downloader.playlist import wants_single_video
from downloader.sizes import estimate_sizes
from settings.user_settings import get_settings

log = logging.getLogger("candy.probe")

# Seconds to wait for the preview before falling back to the plain menu. Real
# downloads get 20 minutes; the old 15/25 s was too tight for a slow
# connection, and the cookie path is slower still (JS challenge solving).
PROBE_TIMEOUT = 30
PROBE_TIMEOUT_WITH_COOKIES = 60


@dataclass
class ProbeResult:
    ok: bool
    title: str = ""
    thumbnail: str = ""
    heights: list[int] = field(default_factory=list)   # available video heights, descending
    has_audio: bool = True                              # any audio-bearing format at all
    is_playlist: bool = False
    # Length in seconds; None for playlists, live streams and anything the
    # site doesn't report. Time-range sections are only offered when this is
    # known (it's also what timestamps are validated against).
    duration: int | None = None
    # Estimated bytes per quality button ("1080", "best", "worst", "mp3", "opus"); see downloader/sizes.py.
    sizes: dict = field(default_factory=dict)
    # [(title, start_s, end_s)] when the video has chapters - one-tap time ranges in the sections editor.
    chapters: list = field(default_factory=list)
    error: str = ""                                     # populated when ok=False, for a better user message


def _safe_sizes(formats: list, duration, heights: list) -> dict:
    """Sizes are a nicety: a surprise in some site's format list must never break the preview."""
    try:
        return estimate_sizes(formats, duration, heights)
    except Exception:  # noqa: BLE001
        log.debug("Size estimation failed", exc_info=True)
        return {}


def _chapters(info: dict, duration) -> list:
    """[(title, start, end)] from the video's chapter list, cleaned: ends are
    clamped to the (whole-second) duration, junk entries dropped."""
    result = []
    for chapter in info.get("chapters") or []:
        try:
            start = float(chapter["start_time"])
            end = float(chapter.get("end_time") or duration or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if duration:
            end = min(end, float(duration))
        if end - start >= 1:
            result.append(((chapter.get("title") or "Chapter")[:80], start, end))
    return result[:200]


async def probe(url: str, user_id: int | None = None) -> ProbeResult:
    """Fast, download-free metadata lookup. Any failure just returns
    ok=False (with the error message attached) so the caller can fall
    back to a generic menu instead of crashing the whole flow over a
    probe hiccup."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "discard_in_playlist",
        "socket_timeout": 8,
        # watch?v=X&list=Y means "this video": don't turn it into a playlist preview.
        "noplaylist": wants_single_video(url),
    }
    if BGUTIL_POT_URL:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [BGUTIL_POT_URL]}}

    # Use the person's cookies for the preview too - without this the
    # preview always ran logged-out and kept asking for a login even after
    # cookies were uploaded.
    cookie_path = cookie_file_for(user_id, get_settings(user_id)) if user_id is not None else None
    if cookie_path:
        apply_cookies(opts, cookie_path)

    def run() -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(loop.run_in_executor(None, run),
                                      timeout=PROBE_TIMEOUT_WITH_COOKIES if cookie_path else PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        # A timeout has an EMPTY message, which used to log as "Probe failed for <url>:" with
        # nothing after it. Cookies make YouTube run the JS challenge solver first, which is
        # slow on a slow connection.
        log.info("Probe timed out for %s after %ss", url, PROBE_TIMEOUT_WITH_COOKIES if cookie_path else PROBE_TIMEOUT)
        return ProbeResult(ok=False, error="timed out")
    except Exception as exc:  # noqa: BLE001
        log.info("Probe failed for %s: %s: %s", url, type(exc).__name__, exc)
        return ProbeResult(ok=False, error=str(exc).strip().splitlines()[0][:200] if str(exc).strip() else "")

    if not info:
        return ProbeResult(ok=False)

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        first = entries[0] if entries else {}
        return ProbeResult(
            ok=True,
            title=info.get("title") or "Playlist",
            thumbnail=first.get("thumbnail") or "",
            heights=[],
            has_audio=True,
            is_playlist=True,
        )

    formats = info.get("formats") or []
    heights = sorted({
        f["height"] for f in formats
        if f.get("height") and f.get("vcodec") not in (None, "none")
    }, reverse=True)
    has_video = bool(heights) or (info.get("vcodec") not in (None, "none"))
    has_audio = any(f.get("acodec") not in (None, "none") for f in formats) or not formats

    # A live stream (or a not-yet-started premiere) has no fixed length, so
    # a time range on it is meaningless even if the site reports something.
    is_live = bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming", "post_live")
    raw_duration = info.get("duration")
    duration = int(raw_duration) if raw_duration and not is_live else None

    return ProbeResult(
        ok=True,
        title=(info.get("title") or "")[:150],
        thumbnail=info.get("thumbnail") or "",
        heights=heights if has_video else [],
        has_audio=has_audio,
        duration=duration,
        sizes=_safe_sizes(formats, duration, heights if has_video else []),
        chapters=_chapters(info, duration),
    )
