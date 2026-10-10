"""
Run ONLY the web app on your own computer, for debugging - no Telegram token, no Docker needed.

    cd bot
    pip install -r requirements.txt        # once (needs ffmpeg on your PATH for real downloads)
    python web_dev.py                      # then open http://localhost:8080

Everything it stores lives in ../dev_data (delete that folder to start fresh). The first time, it creates an admin
account and prints its password once; set WEB_ADMIN_USER / WEB_ADMIN_PASSWORD to choose them. Other knobs:
WEB_PORT (8080), WEB_HOST (127.0.0.1 - keep it that way: this mode has no HTTPS), CANDY_DEV_DIR (data folder).
Downloads are real (yt-dlp), but "Send to Telegram" is not available. Restart it to pick up Python changes; changes
to bot/web/static/* show on a normal page refresh.
"""
import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("CANDY_DEV_DIR") or HERE.parent / "dev_data").resolve()
PORT = int(os.environ.get("WEB_PORT", "8080"))

os.environ.setdefault("BOT_TOKEN", "dev-token-not-used")
os.environ["WEB_ENABLED"] = "on"
os.environ.setdefault("WEB_HOST", "127.0.0.1")
os.environ["WEB_PORT"] = str(PORT)
os.environ["WEB_COOKIE_SECURE"] = "off"          # plain http on localhost
os.environ["WEB_TRUST_PROXY"] = "off"            # nobody in front of us: never believe X-Forwarded-For
os.environ.setdefault("WEB_PUBLIC_URL", f"http://localhost:{PORT}")
sys.path.insert(0, str(HERE))

import config  # noqa: E402  (must be patched BEFORE the modules that copy these paths are imported)

config.DATA_DIR = str(ROOT / "data")
config.TMP_DIR = str(ROOT / "tmp")
config.CACHE_DIR = str(ROOT / "tmp" / "recent")
config.COOKIES_DIR = str(ROOT / "cookies")
config.DB_PATH = os.path.join(config.DATA_DIR, "candy.db")
for folder in (config.DATA_DIR, config.TMP_DIR, config.CACHE_DIR, config.COOKIES_DIR):
    Path(folder).mkdir(parents=True, exist_ok=True)

from jobqueue.job_manager import JobManager  # noqa: E402
from utils.webchat import RoutingBot  # noqa: E402
from web import accounts, server  # noqa: E402


class NoTelegram:
    """Stands in for the Telegram bot: web chats never reach it, anything else is a mistake worth seeing."""
    token = "dev"

    def __getattr__(self, name):
        async def refuse(*args, **kwargs):
            raise RuntimeError(f"Telegram isn't connected in web_dev.py ({name})")
        return refuse


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not (os.environ.get("WEB_ADMIN_USER") and os.environ.get("WEB_ADMIN_PASSWORD")):
        password = secrets.token_urlsafe(12)
        created = accounts.ensure_bootstrap_admin("admin", password)
        if created:
            print(f"\n  First start: sign in as  admin  with password  {password}\n  (shown once - or delete {ROOT} to reset)\n")
    manager = JobManager(RoutingBot(NoTelegram()), config.MAX_CONCURRENT_DOWNLOADS)
    manager.start()
    tasks = await server.start(manager, NoTelegram())
    print(f"  Web app: http://localhost:{PORT}   (Ctrl+C to stop)\n")
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
