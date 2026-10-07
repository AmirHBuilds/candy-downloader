"""
Before showing quality buttons, actually ask yt-dlp what's available for
this specific link, instead of showing a fixed list that may not match
reality (e.g. offering 1080p on a video that tops out at 720p, or
offering quality choices at all on a site that has none).
"""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import yt_dlp

from config import BGUTIL_POT_URL
from downloader.cookies import apply_cookies, cookie_file_for
from downloader.playlist import wants_single_video
from downloader.proxy import policy
from downloader.sizes import describe_best, estimate_sizes
from downloader.subtitles import available_tracks
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
    # [SubTrack] - the subtitle languages on offer, Persian and English first (downloader/subtitles.py).
    subtitles: list = field(default_factory=list)
    error: str = ""                                     # populated when ok=False, for a better user message


HEAD_MAX_FORMATS = 5          # at most this many formats are asked about
HEAD_TIMEOUT = 4              # seconds per request


def _head_size(url: str, headers: dict | None) -> int | None:
    """The size the server itself reports, without downloading: Content-Length from a HEAD
    request, or from a one-byte ranged GET for servers that refuse HEAD."""
    import httpx
    try:
        with httpx.Client(timeout=HEAD_TIMEOUT, follow_redirects=True, headers=headers or {}) as client:
            response = client.head(url)
            length = response.headers.get("content-length", "")
            if response.status_code < 400 and length.isdigit() and int(length) > 0:
                return int(length)
            response = client.get(url, headers={"Range": "bytes=0-0"})
            match = re.search(r"/(\d+)$", response.headers.get("content-range", ""))
            return int(match.group(1)) if match else None
    except Exception:  # noqa: BLE001
        return None


def _fill_missing_sizes(info: dict) -> None:
    """Some sites (Instagram, for one) announce no file sizes and no bitrates, so the quality
    buttons had nothing to estimate from. Ask the server about the few formats that matter - only
    when NOTHING in the list has size information, so sites like YouTube never pay for this."""
    formats = info.get("formats") or []
    if not formats or any(f.get("filesize") or f.get("filesize_approx") or f.get("tbr") for f in formats):
        return
    candidates = [f for f in formats
                  if str(f.get("url", "")).startswith(("http://", "https://"))
                  and not str(f.get("protocol", "")).startswith(("m3u8", "http_dash", "mhtml"))]
    # the best of each height, plus the best audio-only stream
    best_by_height: dict = {}
    for f in sorted(candidates, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True):
        if f.get("vcodec") not in (None, "none") or f.get("acodec") in (None, "none"):
            best_by_height.setdefault(f.get("height") or 0, f)
    audio = next((f for f in candidates if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")), None)
    chosen = list(best_by_height.values())[:HEAD_MAX_FORMATS] + ([audio] if audio else [])
    if not chosen:
        return
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for fmt, size in zip(chosen, pool.map(lambda f: _head_size(f["url"], f.get("http_headers")), chosen)):
                if size:
                    fmt["filesize"] = size
    except Exception:  # noqa: BLE001
        log.debug("Asking for sizes failed", exc_info=True)


def _safe_sizes(formats: list, duration, heights: list) -> dict:
    """Sizes are a nicety: a surprise in some site's format list must never break the preview."""
    try:
        try:
            log.info("Size estimate (best): %s", describe_best(formats, duration))
        except Exception:  # noqa: BLE001
            pass
        return estimate_sizes(formats, duration, heights)
    except Exception:  # noqa: BLE001
        log.debug("Size estimation failed", exc_info=True)
        return {}


def _safe_tracks(info: dict) -> list:
    try:
        return available_tracks(info)
    except Exception:  # noqa: BLE001 - a nicety; a strange subtitle list must never break the preview
        log.debug("Subtitle listing failed", exc_info=True)
        return []


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
    """The preview, routed: direct, or through the proxy when YouTube is blocking this server's address
    (see downloader/proxy.py). A block-type failure is retried once on the new route."""
    explicit = (get_settings(user_id).get("proxy") if user_id is not None else "") or ""
    proxy = explicit or policy.route(url)
    result = await _probe_once(url, user_id, proxy)
    if not explicit:
        if not result.ok and policy.report_failure(url, proxy, result.error):
            proxy = policy.route(url)
            result = await _probe_once(url, user_id, proxy)
        if result.ok:
            policy.report_success(url, proxy)
    return result


async def _probe_once(url: str, user_id: int | None, proxy: str | None) -> ProbeResult:
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
        # Only need to know "this is a playlist", not walk all of it (discarding entries still pages through
        # every one of them, which is what made big playlists time out here).
        "playlistend": 1,
    }
    if proxy:
        opts["proxy"] = proxy
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
            info = ydl.extract_info(url, download=False)
        _fill_missing_sizes(info)
        return info

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
        subtitles=_safe_tracks(info),
    )
