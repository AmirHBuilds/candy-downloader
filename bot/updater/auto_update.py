"""
yt-dlp and gallery-dl get patched constantly as sites change and start
blocking old versions. This runs a daily pip upgrade for both, in-place,
without needing a container rebuild or restart - yt_dlp/gallery_dl are
re-imported fresh on each job run, so a pip upgrade takes effect on the
very next download.
"""
import asyncio
import logging

from telegram import Bot
from telegram.constants import ParseMode

from config import ADMIN_USER_IDS

log = logging.getLogger("candy.updater")

PACKAGES = ["yt-dlp", "gallery-dl"]


async def run_update_once(bot: Bot | None = None, notify_admins: bool = True) -> str:
    results = []
    for pkg in PACKAGES:
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", "--no-cache-dir", "--upgrade", pkg,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        output = out.decode(errors="ignore")
        changed = "Successfully installed" in output
        results.append(f"{'✓' if changed else '·'} {pkg}: {'updated' if changed else 'already latest'}")
        log.info("Update check for %s: %s", pkg, "updated" if changed else "already latest")

    summary = "<b>Daily tool update</b>\n" + "\n".join(results)
    if notify_admins and bot:
        for admin_id in ADMIN_USER_IDS:
            try:
                await bot.send_message(admin_id, summary, parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                log.warning("Could not notify admin %s", admin_id)
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
