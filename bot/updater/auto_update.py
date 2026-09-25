"""
yt-dlp and gallery-dl get patched constantly as sites change and start
blocking old versions. This runs a daily pip upgrade for both, in-place,
without needing a container rebuild.

gallery-dl always runs as a subprocess, so a pip upgrade takes effect on
its very next invocation - nothing else to do there. yt-dlp is different:
it's imported as an in-process library (see downloader/ytdlp_handler.py
and downloader/probe.py), and Python caches an already-imported module in
sys.memory for the life of the process - pip dropping new files on disk
doesn't change what's already loaded. So a yt-dlp upgrade only actually
takes effect once this process restarts. Since docker-compose.yml runs
this service with `restart: unless-stopped`, exiting after a real yt-dlp
version bump is enough to pick it up automatically within a few seconds.
"""
import asyncio
import logging
import os
import sys

from telegram import Bot
from telegram.constants import ParseMode

from config import OWNER_USER_ID

log = logging.getLogger("candy.updater")

PACKAGES = ["yt-dlp", "gallery-dl"]


async def run_update_once(bot: Bot | None = None, notify_admins: bool = True) -> str:
    results = []
    ytdlp_changed = False
    for pkg in PACKAGES:
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", "--no-cache-dir", "--upgrade", pkg,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = out.decode(errors="ignore")
        changed = "Successfully installed" in output
        if pkg == "yt-dlp" and changed:
            ytdlp_changed = True
        results.append(f"{'✓' if changed else '·'} {pkg}: {'updated' if changed else 'already latest'}")
        log.info("Update check for %s: %s", pkg, "updated" if changed else "already latest")

    summary = "<b>Daily tool update</b>\n" + "\n".join(results)
    if ytdlp_changed:
        summary += "\n\nyt-dlp was updated - restarting to pick it up..."

    # notify_admins is a historical name for "send the summary at all" -
    # this is a maintenance notice about the bot's own dependencies, not
    # something every added admin needs pinged about, so it goes to the
    # owner only.
    if notify_admins and bot and OWNER_USER_ID:
        try:
            await bot.send_message(OWNER_USER_ID, summary, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001
            log.warning("Could not notify owner %s", OWNER_USER_ID)

    if ytdlp_changed:
        log.warning("yt-dlp was upgraded - exiting so `restart: unless-stopped` picks up the new version.")
        if bot and OWNER_USER_ID and not notify_admins:
            try:
                await bot.send_message(OWNER_USER_ID, summary, parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                log.warning("Could not notify owner %s", OWNER_USER_ID)
        await asyncio.sleep(2)  # give the notify message a moment to actually send
        os._exit(0)  # a clean sys.exit() would let asyncio.run_polling's own shutdown handling race this

    return summary


async def daily_update_loop(bot: Bot, hour_utc: int) -> None:
    """Sleeps until the configured UTC hour each day, then updates."""
    import datetime as dt

    while True:
        now = dt.datetime.utcnow()
        target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await run_update_once(bot)
        except Exception:  # noqa: BLE001
            log.exception("Scheduled update failed")
