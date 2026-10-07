"""
What is inside a playlist, and what are these pasted links?

Feeds the multi-select picker (see jobqueue/batch.py and ui/batch_menu.py).

The key rule is wants_single_video(): a link like watch?v=X&list=Y is almost
always "this one video" (people copy it from inside a playlist, or from a
"Mix" that never ends), so it must keep downloading just that video. Only a
link that is really about the playlist opens the picker.
"""
import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import yt_dlp

from config import BGUTIL_POT_URL
from downloader.cookies import apply_cookies, cookie_file_for
from downloader.proxy import policy
from settings.user_settings import get_settings

log = logging.getLogger("candy.playlist")

MAX_LISTED = 200            # most items we list from one playlist
LIST_TIMEOUT = 60
LIST_TIMEOUT_WITH_COOKIES = 90
TITLES_TIMEOUT = 25         # for a batch of pasted links: whatever hasn't answered by then gets a plain label
TITLES_CONCURRENCY = 4
_UNAVAILABLE = {"[private video]", "[deleted video]"}


class ListingError(Exception):
    """Could not list the playlist; the message is safe to show the person."""


@dataclass
class Entry:
    url: str
    title: str
    duration: int | None = None


# ---------------------------------------------------------------- pure helpers
def _is_youtube(host: str) -> bool:
    return host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com")


def wants_single_video(url: str) -> bool:
    """True when the link points at ONE video even if it also names a playlist.
    youtu.be/ID, watch?v=ID, /shorts/, /live/, /embed/ are single videos;
    /playlist?list=... and watch?list=... (no v=) are playlists. Other sites
    are left to yt-dlp to classify (False)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not _is_youtube(host):
        return False
    if host == "youtu.be":
        return True
    if parsed.path.rstrip("/") == "/playlist":
        return False
    if parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
        return True
    return "v" in parse_qs(parsed.query)


def looks_like_playlist(url: str) -> bool:
    """A link that is plainly a YouTube playlist (/playlist?list=..., or watch?list=... with no video).
    These skip the single-video preview entirely and go straight to the item list."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return _is_youtube(host) and "list" in parse_qs(parsed.query) and not wants_single_video(url)


def entry_url(entry: dict) -> str | None:
    """A downloadable URL for one flat playlist entry, or None."""
    url = entry.get("webpage_url") or entry.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    if entry.get("id") and str(entry.get("ie_key") or "").lower() == "youtube":
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return None


def short_label(url: str) -> str:
    """A readable stand-in title when we couldn't fetch the real one."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url[:40]
    host = (parsed.hostname or "").removeprefix("www.")
    tail = (parsed.path.rstrip("/").rsplit("/", 1)[-1] or "") if host != "youtu.be" else parsed.path.strip("/")
    video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    tail = video_id or tail
    return f"{host}/{tail}"[:40] if tail else host


def entries_from_info(info: dict, limit: int = MAX_LISTED) -> list[Entry]:
    result = []
    for raw in info.get("entries") or []:
        if not raw:
            continue
        url = entry_url(raw)
        title = (raw.get("title") or "").strip()
        if not url or title.lower() in _UNAVAILABLE:
            continue                                       # private / deleted: nothing to download
        duration = raw.get("duration")
        result.append(Entry(url, title or short_label(url), int(duration) if duration else None))
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------- yt-dlp lookups
def _base_opts(user_id: int | None, **extra) -> tuple[dict, bool]:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15, **extra}
    if BGUTIL_POT_URL:
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [BGUTIL_POT_URL]}}
    cookie_path = cookie_file_for(user_id, get_settings(user_id)) if user_id is not None else None
    if cookie_path:
        apply_cookies(opts, cookie_path)
    return opts, bool(cookie_path)


async def list_playlist(url: str, user_id: int | None = None, limit: int = MAX_LISTED) -> tuple[str, list[Entry]]:
    """(playlist title, entries), routed like the preview (direct, or via the proxy when YouTube is blocking
    this address); a block-type failure is retried once on the new route."""
    explicit = (get_settings(user_id).get("proxy") if user_id is not None else "") or ""
    proxy = explicit or policy.route(url)
    try:
        result = await _list_once(url, user_id, limit, proxy)
    except ListingError as exc:
        if explicit or not policy.report_failure(url, proxy, str(exc.__cause__ or "")):
            raise
        proxy = policy.route(url)
        result = await _list_once(url, user_id, limit, proxy)
    if not explicit:
        policy.report_success(url, proxy)
    return result


async def _list_once(url: str, user_id: int | None, limit: int, proxy: str | None) -> tuple[str, list[Entry]]:
    """Flat extraction: titles and durations only, without opening every video, so even a long
    playlist lists in seconds."""
    opts, with_cookies = _base_opts(user_id, extract_flat="in_playlist", playlistend=limit)
    if proxy:
        opts["proxy"] = proxy

    def run() -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_running_loop()
    try:
        info = await asyncio.wait_for(loop.run_in_executor(None, run),
                                      timeout=LIST_TIMEOUT_WITH_COOKIES if with_cookies else LIST_TIMEOUT)
    except asyncio.TimeoutError:
        log.info("Listing %s timed out", url)
        raise ListingError("The playlist took too long to load.")
    except Exception as exc:  # noqa: BLE001
        log.info("Listing %s failed: %s: %s", url, type(exc).__name__, exc)
        raise ListingError("Couldn't read this playlist.") from exc
    entries = entries_from_info(info or {}, limit)
    if not entries:
        raise ListingError("That playlist has nothing downloadable in it.")
    return (info.get("title") or "Playlist")[:100], entries


async def quick_titles(urls: list[str], user_id: int | None = None) -> list[Entry]:
    """A title (and length) for each pasted link, looked up a few at a time.
    Anything that fails or hasn't answered within TITLES_TIMEOUT just gets a
    plain label - the picker must never wait on one slow link."""
    opts, _ = _base_opts(user_id, extract_flat="discard_in_playlist", noplaylist=True, socket_timeout=10)
    loop = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(TITLES_CONCURRENCY)

    def run(url: str) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def one(url: str) -> Entry:
        async with semaphore:
            try:
                info = await loop.run_in_executor(None, run, url)
            except Exception:  # noqa: BLE001
                return Entry(url, short_label(url))
        duration = (info or {}).get("duration")
        return Entry(url, ((info or {}).get("title") or short_label(url))[:150], int(duration) if duration else None)

    tasks = [asyncio.create_task(one(url)) for url in urls]
    done, pending = await asyncio.wait(tasks, timeout=TITLES_TIMEOUT)
    for task in pending:
        task.cancel()
    return [task.result() if task in done else Entry(url, short_label(url)) for task, url in zip(tasks, urls)]
