import logging
from pathlib import Path
from typing import Callable

from downloader import ytdlp_handler, gallerydl_handler, generic_handler, spotify_handler
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


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                    progress_cb: Callable[[str, float | None, str | None, str | None, str | None], None]
                    ) -> list[Path]:
    """Tries each candidate tool in priority order for this URL's domain.

    Reports each real transition as its own step via progress_cb(..., stage=...)
    - "Trying X...", "X failed: ...", etc. - instead of letting a tool's own
    placeholder/fake progress bar keep climbing while it's actually about
    to fail. That mismatch (a bar showing movement while the tool is
    already doomed) was confusing and dishonest.

    Raises NoToolSucceeded if every candidate fails."""
    order = tool_order_for(url)
    attempts: dict[str, str] = {}

    for tool_name in order:
        handler = HANDLERS[tool_name]
        label = FRIENDLY_TOOL.get(tool_name, tool_name)
        progress_cb(tool_name, None, None, None, f"Trying {label}...")
        try:
            log.info("Trying %s for %s", tool_name, url)

            def cb(percent, speed, eta, stage=None, _tool=tool_name):
                progress_cb(_tool, percent, speed, eta, stage)

            files = await handler(url, workspace, settings, user_id, cb)
            if files:
                return files
            attempts[tool_name] = "produced no files"
            progress_cb(tool_name, None, None, None, f"{label}: produced no files")
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
