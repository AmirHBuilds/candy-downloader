import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

from config import COOKIES_DIR
from downloader.errors import JobCancelled

log = logging.getLogger("candy.gallerydl")

ProgressCB = Callable[[float | None, str | None, str | None, str | None], None]

DOWNLOAD_TIMEOUT_SECONDS = 20 * 60


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                    progress_cb: ProgressCB, cancel_event: asyncio.Event | None = None) -> list[Path]:
    """gallery-dl doesn't expose granular byte-level progress. Rather than
    faking a percentage that climbs regardless of whether the download is
    actually succeeding (misleading if it's about to fail), this shows
    real elapsed time instead - honest, and still proves it's alive."""
    cmd = [
        "gallery-dl",
        "--dest", str(workspace),
        "-o", "skip=abort",
    ]

    cookie_file = Path(COOKIES_DIR) / f"{user_id}.txt"
    if settings["cookies_enabled"] and cookie_file.exists():
        cmd += ["--cookies", str(cookie_file)]

    if settings["proxy"]:
        cmd += ["--proxy", settings["proxy"]]

    cmd.append(url)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def heartbeat() -> None:
        start = time.monotonic()
        while process.returncode is None:
            elapsed = int(time.monotonic() - start)
            progress_cb(None, f"{elapsed}s elapsed", None, None)
            await asyncio.sleep(2.0)

    cancelled = False

    async def cancel_watcher() -> None:
        nonlocal cancelled
        if cancel_event is None:
            return
        await cancel_event.wait()
        cancelled = True
        if process.returncode is None:
            process.kill()

    heartbeat_task = asyncio.create_task(heartbeat())
    watcher_task = asyncio.create_task(cancel_watcher())
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"gallery-dl timed out after {DOWNLOAD_TIMEOUT_SECONDS // 60} minutes")
    finally:
        heartbeat_task.cancel()
        watcher_task.cancel()

    if cancelled:
        raise JobCancelled("Cancelled by user")

    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore") or stdout.decode(errors="ignore"))

    files = [p for p in sorted(workspace.rglob("*")) if p.is_file()]
    if not files:
        raise RuntimeError("gallery-dl finished but produced no files")
    return files
