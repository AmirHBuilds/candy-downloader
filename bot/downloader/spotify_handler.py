"""
Spotify's actual audio streams are DRM-protected - yt-dlp deliberately
refuses them outright, and no tool can fix that; it's not a bug to work
around. The standard approach (used by every real "Spotify downloader")
is: read the track's title/artist from Spotify's public oEmbed endpoint
(no login needed), then search YouTube for a matching upload and download
that audio instead.
"""
import logging
from pathlib import Path
from typing import Callable

import httpx

from downloader import ytdlp_handler

log = logging.getLogger("candy.spotify")

ProgressCB = Callable[[float, str | None, str | None], None]


async def get_track_title(url: str) -> str | None:
    info = await get_track_info(url)
    return info.get("title") if info else None


async def get_track_info(url: str) -> dict | None:
    """Spotify's oEmbed endpoint needs no auth and works for track/album/
    playlist links. Returns {"title":..., "thumbnail":...}, or None."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            resp = await client.get("https://open.spotify.com/oembed", params={"url": url})
            resp.raise_for_status()
            data = resp.json()
            title = data.get("title")
            if not title:
                return None
            return {"title": title, "thumbnail": data.get("thumbnail_url") or ""}
    except Exception as exc:  # noqa: BLE001
        log.info("Spotify oEmbed lookup failed for %s: %s", url, exc)
        return None


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                    progress_cb: ProgressCB) -> list[Path]:
    title = await get_track_title(url)
    if not title:
        raise RuntimeError(
            "Couldn't read this Spotify link's track info - it may be "
            "private, region-locked, or not a track/album link."
        )

    audio_settings = dict(settings)
    audio_settings["mode"] = "audio"
    search_query = f"ytsearch1:{title} audio"
    log.info("Spotify track %r -> searching YouTube: %s", title, search_query)
    return await ytdlp_handler.download(search_query, workspace, audio_settings, user_id, progress_cb)
