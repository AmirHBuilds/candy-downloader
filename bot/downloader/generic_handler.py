import asyncio
import logging
import re
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("candy.generic")

ProgressCB = Callable[[float, str | None, str | None], None]

DOWNLOAD_TIMEOUT_SECONDS = 20 * 60

# aria2c prints progress lines like:
# [#1a2b3c 12MiB/50MiB(24%) CN:4 DL:2.1MiB ETA:18s]
_PROGRESS_RE = re.compile(r"\((\d+)%\).*?DL:([\d.]+\w+/?s?).*?ETA:(\S+)")

_HTML_SNIFF = (b"<!doctype html", b"<html", b"<head", b"<!DOCTYPE HTML")


async def _looks_like_a_webpage(url: str) -> bool:
    """Quick sanity check before we bother downloading: if the server
    says this is an HTML page rather than a file, it's not something
    aria2c should be grabbing - it means no real "direct file" exists
    here (most likely the URL isn't actually a supported media link)."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8) as client:
            resp = await client.head(url)
            content_type = resp.headers.get("content-type", "")
            if not content_type:
                # some servers don't answer HEAD properly - fall back to a tiny GET
                resp = await client.get(url, headers={"Range": "bytes=0-512"})
                content_type = resp.headers.get("content-type", "")
            return "text/html" in content_type.lower()
    except Exception:  # noqa: BLE001
        return False  # can't tell - let aria2c try, we'll sniff the bytes after


STALL_TIMEOUT_SECONDS = 45  # no output at all for this long -> probably stuck/blocked, fail fast


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                    progress_cb: ProgressCB) -> list[Path]:
    """Last-resort downloader for plain direct file links (pdf, zip, mp4
    hosted directly, etc.) using aria2c for fast multi-connection fetching.
    Refuses to accept an HTML page as a "successful" download."""
    if await _looks_like_a_webpage(url):
        raise RuntimeError(
            "This link points to a webpage, not a downloadable file - "
            "no supported extractor recognized it."
        )

    cmd = [
        "aria2c",
        "--dir", str(workspace),
        "--max-connection-per-server=8" if not settings["rate_limit_kbps"] else "--max-connection-per-server=4",
        "--split=8",
        "--summary-interval=1",
        "--console-log-level=warn",
        # native timeouts so aria2c itself can't hang forever on a bad connection
        "--timeout=30",
        "--connect-timeout=15",
        "--max-tries=5",
    ]
    if settings["rate_limit_kbps"]:
        cmd.append(f"--max-download-limit={int(settings['rate_limit_kbps'])}K")
    if settings["proxy"]:
        cmd.append(f"--all-proxy={settings['proxy']}")
    cmd.append(url)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def read_output():
        assert process.stdout
        while True:
            try:
                line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=STALL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"No response for {STALL_TIMEOUT_SECONDS}s - the server "
                    "isn't sending anything (likely blocking direct downloads)"
                )
            if not line_bytes:  # EOF - process finished
                return
            line = line_bytes.decode(errors="ignore")
            match = _PROGRESS_RE.search(line)
            if match:
                percent = float(match.group(1))
                progress_cb(percent, match.group(2), match.group(3))

    try:
        await asyncio.wait_for(read_output(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
        await asyncio.wait_for(process.wait(), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Timed out after {DOWNLOAD_TIMEOUT_SECONDS // 60} minutes - the connection was too slow/stuck")
    except RuntimeError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        raise RuntimeError(f"aria2c exited with code {process.returncode} — link may not be a direct file")

    progress_cb(100.0, None, None)
    files = [p for p in sorted(workspace.iterdir()) if p.is_file() and not p.name.endswith(".aria2")]
    if not files:
        raise RuntimeError("Download finished but no file was produced")

    # Final safety net: sniff the actual bytes in case content-type lied.
    for f in files:
        with open(f, "rb") as fh:
            head = fh.read(256).lower()
        if any(marker.lower() in head for marker in _HTML_SNIFF):
            f.unlink(missing_ok=True)
            raise RuntimeError("Downloaded content was an HTML page, not a media file")

    return files
