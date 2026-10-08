import logging
import shutil
from pathlib import Path
from typing import Callable

from downloader import ytdlp_handler, gallerydl_handler, generic_handler, spotify_handler
from downloader.errors import JobCancelled  # noqa: F401 - re-exported for callers
from downloader.proxy import policy, site_label
from downloader.site_map import tool_order_for

log = logging.getLogger("candy.dispatcher")

HANDLERS = {
    "ytdlp": ytdlp_handler.download,
    "gallerydl": gallerydl_handler.download,
    "generic": generic_handler.download,
    "spotify": spotify_handler.download,
}

FRIENDLY_TOOL = {
    "ytdlp": "yt-dlp",
    "gallerydl": "gallery-dl",
    "generic": "direct download",
    "spotify": "Spotify search",
}


class NoToolSucceeded(Exception):
    def __init__(self, attempts: dict[str, str], primary_tool: str):
        self.attempts = attempts
        self.primary_tool = primary_tool
        super().__init__("; ".join(f"{k}: {v}" for k, v in attempts.items()))

    @property
    def primary_error(self) -> str:
        """A single clean line for the user - the primary (first-choice)
        tool's own error, not a concatenation of every tool that was tried.
        That concatenation was producing garbled, misleading messages."""
        raw = self.attempts.get(self.primary_tool) or next(iter(self.attempts.values()), "")
        first_line = raw.strip().splitlines()[0] if raw.strip() else "Unknown error"
        return first_line[:200]


def _clear(workspace: Path) -> None:
    for leftover in workspace.iterdir():
        shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink(missing_ok=True)


async def _routed(handler, url: str, workspace: Path, settings: dict, user_id: int, cb, cancel_event):
    """Run a tool that has no routing of its own (gallery-dl) through the same block-aware proxy logic
    yt-dlp uses: direct first; if the site refuses this server's address, once more through the proxy.
    Someone's own proxy setting bypasses it. (aria2c is never routed here: it cannot speak SOCKS.)"""
    if settings.get("proxy"):
        return await handler(url, workspace, settings, user_id, cb, cancel_event=cancel_event)
    proxy = policy.route(url)
    for attempt in (1, 2):
        trial = {**settings, "proxy": proxy} if proxy else settings
        try:
            files = await handler(url, workspace, trial, user_id, cb, cancel_event=cancel_event)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == 1 and policy.report_failure(url, proxy, str(exc)):
                cb(None, None, None, f"Switching route - {site_label(url)} blocked this address")
                _clear(workspace)
                proxy = policy.route(url)
                continue
            raise
        policy.report_success(url, proxy)
        return files


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                    progress_cb: Callable[[str, float | None, str | None, str | None, str | None], None],
                    cancel_event=None,
                    ) -> list[Path]:
    """Tries each candidate tool in priority order for this URL's domain.

    Reports each real transition as its own step via progress_cb(..., stage=...)
    - "Trying X...", "X failed: ...", etc. - instead of letting a tool's own
    placeholder/fake progress bar keep climbing while it's actually about
    to fail. That mismatch (a bar showing movement while the tool is
    already doomed) was confusing and dishonest.

    Raises NoToolSucceeded if every candidate fails."""
    order = tool_order_for(url)
    if settings.get("prefer_ytdlp") and "ytdlp" in order:
        # The person chose a quality / time range / subtitles from a yt-dlp preview (on a site that
        # normally tries gallery-dl first, like X): honour that by trying yt-dlp first.
        order = ["ytdlp"] + [tool for tool in order if tool != "ytdlp"]
    if settings.get("prefer_gallerydl") and "gallerydl" in order:
        # The person pressed the plain "Download" button of an image post (no video preview existed).
        order = ["gallerydl"] + [tool for tool in order if tool != "gallerydl"]
    attempts: dict[str, str] = {}

    if settings.get("sections"):
        # Only yt-dlp can cut a time range. Falling through to gallery-dl or
        # aria2c would quietly download the WHOLE file - exactly what the
        # person asked not to do - so a clip request never uses them.
        if "ytdlp" not in order:
            raise NoToolSucceeded({order[0]: "time ranges aren't supported for this link"}, primary_tool=order[0])
        order = ["ytdlp"]

    for tool_name in order:
        handler = HANDLERS[tool_name]
        label = FRIENDLY_TOOL.get(tool_name, tool_name)
        progress_cb(tool_name, None, None, None, f"Trying {label}...")
        try:
            log.info("Trying %s for %s", tool_name, url)

            def cb(percent, speed, eta, stage=None, label=None, size=None, _tool=tool_name):
                progress_cb(_tool, percent, speed, eta, stage, label, size)

            if tool_name == "gallerydl":
                files = await _routed(handler, url, workspace, settings, user_id, cb, cancel_event)
            else:
                files = await handler(url, workspace, settings, user_id, cb, cancel_event=cancel_event)
            if files:
                return files
            attempts[tool_name] = "produced no files"
            progress_cb(tool_name, None, None, None, f"{label}: produced no files")
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - we want to try the next tool regardless of cause
            log.warning("%s failed for %s: %s", tool_name, url, exc)
            attempts[tool_name] = str(exc)
            short = str(exc).strip().splitlines()[0] if str(exc).strip() else "failed"
            progress_cb(tool_name, None, None, None, f"{label} failed: {short}")
            # clean any partial junk before the next tool tries
            for leftover in workspace.iterdir():
                try:
                    if leftover.is_file():
                        leftover.unlink()
                except OSError:
                    pass

    raise NoToolSucceeded(attempts, primary_tool=order[0])
