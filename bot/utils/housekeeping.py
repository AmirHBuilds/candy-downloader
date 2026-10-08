"""
Periodic housekeeping, so nothing a job (or a person's upload) leaves behind stays on the server:

  1. orphaned processes   ffmpeg / ffprobe / aria2c / gallery-dl pointing into a job folder that is no longer an
                          active job (the per-job reaping in utils/cleanup is the first line; this is the net).
  2. zombie processes     finished children nobody collected.
  3. the Bot API server's copies of files people sent (they pile up in its volume otherwise).
  4. expired toolbox uploads.

Each step is independent: one failing never stops the others, and nothing here can raise out of the loop.
"""
import asyncio
import logging

import config
from utils import cleanup
from utils.procs import ZombieReaper, kill_orphans

log = logging.getLogger("candy.housekeeping")

reaper = ZombieReaper()


def run_once() -> dict:
    """One pass. Blocking (reads /proc and the disk); run it in a thread."""
    result = {"orphans": 0, "zombies_reaped": 0, "zombies": 0, "server_files": 0, "server_bytes": 0}
    try:
        result["orphans"] = kill_orphans(config.TMP_DIR, cleanup.ACTIVE_WORKSPACES)
    except Exception:  # noqa: BLE001
        log.warning("Orphan sweep failed", exc_info=True)
    try:
        result["zombies_reaped"], result["zombies"] = reaper.sweep()
    except Exception:  # noqa: BLE001
        log.warning("Zombie sweep failed", exc_info=True)
    if config.BOT_API_CLEANUP:
        try:
            result["server_files"], result["server_bytes"] = cleanup.sweep_bot_api_files(
                config.BOT_API_DATA_DIR, config.BOT_API_FILE_MAX_AGE_MINUTES * 60)
        except Exception:  # noqa: BLE001
            log.warning("Bot API storage sweep failed", exc_info=True)
    return result


def summary(result: dict) -> str:
    parts = []
    if result["orphans"]:
        parts.append(f"killed {result['orphans']} orphaned process(es)")
    if result["zombies_reaped"]:
        parts.append(f"collected {result['zombies_reaped']} zombie(s)")
    left = result["zombies"] - result["zombies_reaped"]
    if left > 0:
        parts.append(f"{left} zombie(s) not ours to collect (Docker's init reaps them; check `init: true`)")
    if result["server_files"]:
        parts.append(f"removed {result['server_files']} old file(s) ({result['server_bytes'] / 1_000_000:.0f} MB) "
                     f"from the Bot API storage")
    return ", ".join(parts)


async def loop(extra=None) -> None:
    """Forever: wait, then one pass. `extra` is an optional extra callable to run each time (e.g. expiring uploads)."""
    while True:
        await asyncio.sleep(config.HOUSEKEEPING_INTERVAL_SECONDS)
        try:
            result = await asyncio.to_thread(run_once)
            if extra:
                extra()
            text = summary(result)
            if text:
                log.info("Housekeeping: %s", text)
        except Exception:  # noqa: BLE001
            log.warning("Housekeeping pass failed", exc_info=True)
