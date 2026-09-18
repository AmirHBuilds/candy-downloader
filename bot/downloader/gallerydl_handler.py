import asyncio
import logging
from pathlib import Path
from typing import Callable

from config import COOKIES_DIR

log = logging.getLogger("candy.gallerydl")

ProgressCB = Callable[[float, str | None, str | None], None]


async def download(url: str, workspace: Path, settings: dict, user_id: int,
                    progress_cb: ProgressCB) -> list[Path]:
    """gallery-dl doesn't expose a granular progress API as easily as
    yt-dlp, so we report indeterminate progress (pulses) while its CLI
    process runs, then flip to 100% on completion."""
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

    async def pulse() -> None:
        pct = 0.0
        while process.returncode is None:
            pct = min(90.0, pct + 7.0)
            progress_cb(pct, None, None)
            await asyncio.sleep(1.2)

    pulse_task = asyncio.create_task(pulse())
    stdout, stderr = await process.communicate()
    pulse_task.cancel()

    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore") or stdout.decode(errors="ignore"))

    progress_cb(100.0, None, None)
    files = [p for p in sorted(workspace.rglob("*")) if p.is_file()]
    if not files:
        raise RuntimeError("gallery-dl finished but produced no files")
    return files
