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

log = logging.getLogger("candy.probe")


@dataclass
class ProbeResult:
    ok: bool
    title: str = ""
    thumbnail: str = ""
    heights: list[int] = field(default_factory=list)   # available video heights, descending
    has_audio: bool = True                              # any audio-bearing format at all
    is_playlist: bool = False


async def probe(url: str) -> ProbeResult:
    """Fast, download-free metadata lookup. Any failure just returns
    ok=False so the caller can fall back to a generic menu instead of
    crashing the whole flow over a probe hiccup."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "discard_in_playlist",
        "socket_timeout": 8,
    }
    if BGUTIL_POT_URL:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [BGUTIL_POT_URL]}}

    def run() -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(loop.run_in_executor(None, run), timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.info("Probe failed for %s: %s", url, exc)
        return ProbeResult(ok=False)

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

    return ProbeResult(
        ok=True,
        title=(info.get("title") or "")[:150],
        thumbnail=info.get("thumbnail") or "",
        heights=heights if has_video else [],
        has_audio=has_audio,
    )
