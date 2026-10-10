"""Starts the web server inside the bot's own event loop (so it shares the one JobManager)."""
import asyncio
import contextlib
import logging
from pathlib import Path

import uvicorn

import config
from utils.webchat import RoutingBot  # noqa: F401  (re-exported for main)
from web import accounts, shares
from web.app import Context, build_app
from web.jobs import WebJobs

log = logging.getLogger("candy.web")


class _Server(uvicorn.Server):
    """Run inside someone else's loop: leave signal handling to the bot."""

    def install_signal_handlers(self) -> None:          # older uvicorn
        pass

    @contextlib.contextmanager
    def capture_signals(self):                          # newer uvicorn
        yield


def make_jobs(manager) -> WebJobs:
    return WebJobs(manager, ttl_seconds=config.WEB_FILE_TTL_MINUTES * 60, quota_bytes=config.WEB_QUOTA_MB * 1_000_000,
                   max_active=config.WEB_MAX_ACTIVE_JOBS, root=Path(config.TMP_DIR) / "web")


async def start(manager, bot) -> list[asyncio.Task]:
    """Create the bootstrap admin, build the app and serve it. Returns the background tasks."""
    if config.WEB_ADMIN_USER and config.WEB_ADMIN_PASSWORD:
        try:
            created = await asyncio.to_thread(accounts.ensure_bootstrap_admin, config.WEB_ADMIN_USER,
                                              config.WEB_ADMIN_PASSWORD)
            if created:
                log.info("Created the first web admin '%s'. Remove WEB_ADMIN_PASSWORD from .env now.", created.username)
        except accounts.AccountError as exc:
            log.error("Could not create the web admin from .env: %s", exc)

    async def send_to_telegram(telegram_id: int, path: Path, name: str) -> None:
        with open(path, "rb") as fh:
            await bot.send_document(telegram_id, fh, filename=name, read_timeout=600, write_timeout=600,
                                    connect_timeout=60)

    jobs = make_jobs(manager)
    ctx = Context(jobs, send_to_telegram=send_to_telegram)
    server = _Server(uvicorn.Config(build_app(ctx), host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning",
                                    access_log=False, server_header=False, date_header=False, lifespan="off",
                                    proxy_headers=False, timeout_keep_alive=30))
    if not config.WEB_COOKIE_SECURE:
        log.warning("WEB_COOKIE_SECURE is off: sessions work over plain http. Use this only for local testing.")
    log.info("Web app listening on %s:%s", config.WEB_HOST, config.WEB_PORT)
    async def sweep_shares() -> None:
        while True:
            try:
                await asyncio.to_thread(shares.sweep)
            except Exception:  # noqa: BLE001
                log.exception("Sweeping expired links failed")
            await asyncio.sleep(600)

    return [asyncio.create_task(server.serve()), asyncio.create_task(jobs.sweep_loop()),
            asyncio.create_task(sweep_shares())]
