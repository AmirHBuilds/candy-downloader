"""
CandyDownloader's job manager.

Every incoming link becomes a Job and goes into an asyncio.Queue. A fixed
pool of worker tasks pulls jobs off the queue and processes them, so:

  - the bot's message handler returns instantly (never blocks on a download)
  - at most MAX_CONCURRENT_DOWNLOADS run at once, protecting the VPS
  - progress-bar edits happen on their own throttled schedule and never
    stall the download itself

STEP LOG: instead of one static line ("Preparing...", "Downloading..."),
the status message shows a small rolling log of the last 3 real events
(which tool is being tried, postprocessing stages, etc.) - each genuine
transition pushes a new line; a bare percent tick updates the current
line in place so the bar animates smoothly without spamming new lines.
This also means a failing attempt is reported as what it actually is
("gallery-dl: Unsupported URL") instead of a progress bar that keeps
climbing right up until the failure.
"""
import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from telegram import Bot, InputFile, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import OWNER_EMOJI
from downloader.dispatcher import download as dispatch_download, NoToolSucceeded
from settings import access_control as ac
from ui import messages
from ui.progress import render_bar
from ui.quick_menu import queued_menu, send_as_file_menu, retry_menu
from utils.cleanup import job_workspace, new_cache_path

log = logging.getLogger("candy.jobs")

MIN_EDIT_INTERVAL_SEC = 1.5
MIN_PERCENT_DELTA = 3.0
HEARTBEAT_INTERVAL_SEC = 2.5
SEND_TICKER_INTERVAL_SEC = 2.0
RECENT_FILE_TTL_SECONDS = 5 * 60
MAX_STEP_LINES = 3

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm"}

TOOL_LABEL = {"ytdlp": "Downloading video", "gallerydl": "Downloading gallery",
              "generic": "Downloading file", "spotify": "Finding & downloading track"}

QUEUED_MARKUP = queued_menu()


def _friendly_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host or "this link"
    except Exception:  # noqa: BLE001
        return "this link"


@dataclass
class Job:
    job_id: str
    user_id: int
    chat_id: int
    url: str
    settings: dict
    status_message_id: int
    is_photo: bool = False
    force_document: bool = False
    steps: list[str] = field(default_factory=list)
    header: str = "Working"
    task: Optional[asyncio.Task] = field(default=None)
    cancelled: bool = False


class JobManager:
    def __init__(self, bot: Bot, max_concurrent: int):
        self.bot = bot
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._max_concurrent = max_concurrent
        self._workers: list[asyncio.Task] = []
        self._jobs_by_user: dict[int, Job] = {}
        self._recent_files: dict[int, dict] = {}

    def start(self) -> None:
        for i in range(self._max_concurrent):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))
        log.info("Started %d download workers", self._max_concurrent)

    async def enqueue(self, user_id: int, chat_id: int, url: str, settings: dict,
                       status_message_id: int, is_photo: bool = False,
                       force_document: bool = False) -> Job:
        job = Job(
            job_id=uuid.uuid4().hex[:8],
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            settings=settings,
            status_message_id=status_message_id,
            is_photo=is_photo,
            force_document=force_document,
            header="Queued",
        )
        self._jobs_by_user[user_id] = job
        await self._queue.put(job)
        asyncio.create_task(self._queued_heartbeat(job))
        return job

    def cancel_for_user(self, user_id: int) -> bool:
        job = self._jobs_by_user.get(user_id)
        if not job or job.cancelled:
            return False
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
        return True

    def active_summary(self) -> str:
        if not self._jobs_by_user:
            return "Nothing in the queue right now."
        lines = [f"{OWNER_EMOJI} <b>Current queue</b>"]
        for job in self._jobs_by_user.values():
            state = "running" if job.task and not job.task.done() else "waiting"
            lines.append(f"• <code>{job.url[:40]}</code> — {state}")
        return "\n".join(lines)

    def _render(self, job: Job) -> str:
        lines = [f"{OWNER_EMOJI} <b>{job.header}</b>"]
        if job.steps:
            lines.append("\n".join(f"• {s}" for s in job.steps[-MAX_STEP_LINES:]))
        lines.append(f"<code>{job.url}</code>")
        return "\n\n".join(lines)

    async def _emit(self, job: Job, text: str, push: bool = True,
                     markup: InlineKeyboardMarkup | None = None) -> None:
        if push or not job.steps:
            job.steps.append(text)
            if len(job.steps) > MAX_STEP_LINES:
                job.steps.pop(0)
        else:
            job.steps[-1] = text
        await self._safe_edit(job, self._render(job), markup=markup)

    async def _cache_video_file(self, user_id: int, url: str, src: Path) -> None:
        """Keeps one copy of a just-downloaded video around briefly so
        "send as file" and "try again after a send failure" don't need a
        fresh download.

        Uses shutil.copy (not copy2): copy2 also copies file metadata,
        and that extra step can throw on some Docker-Desktop bind-mounted
        filesystems (notably Windows) even though the content copied
        fine - plain copy avoids that class of false failure entirely.
        """
        old = self._recent_files.pop(user_id, None)
        if old:
            old["path"].unlink(missing_ok=True)

        cached_path = new_cache_path(src.suffix)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, shutil.copy, src, cached_path)
        except OSError as exc:
            log.warning("Could not cache %s -> %s for reuse: %s", src, cached_path, exc)
            return

        self._recent_files[user_id] = {"path": cached_path, "url": url}
        log.info("Cached %s for user %s (url=%s)", cached_path, user_id, url)
        asyncio.create_task(self._expire_cached_file(user_id, cached_path))

    async def _expire_cached_file(self, user_id: int, path: Path) -> None:
        await asyncio.sleep(RECENT_FILE_TTL_SECONDS)
        entry = self._recent_files.get(user_id)
        if entry and entry["path"] == path:
            self._recent_files.pop(user_id, None)
        path.unlink(missing_ok=True)

    def has_cached_video(self, user_id: int, url: str) -> bool:
        entry = self._recent_files.get(user_id)
        hit = bool(entry and entry["url"] == url and entry["path"].exists())
        if entry and not hit:
            log.info("Cache miss for user %s: have url=%r, wanted url=%r", user_id, entry.get("url"), url)
        return hit

    async def send_cached_as_document(self, user_id: int, chat_id: int, url: str,
                                       status_message_id: int) -> bool:
        entry = self._recent_files.get(user_id)
        if not entry or entry["url"] != url or not entry["path"].exists():
            return False

        path = entry["path"]
        caption = messages.all_done_caption(path.stem[:100]) + "\n\nSent as a file — not re-compressed by Telegram."
        try:
            with open(path, "rb") as fh:
                input_file = InputFile(fh, filename=f"video{path.suffix}")
                await self.bot.send_document(chat_id, input_file, caption=caption, parse_mode=ParseMode.HTML,
                                              read_timeout=180, write_timeout=180, connect_timeout=60)
        except (OSError, TelegramError) as exc:
            log.warning("Cached send failed for user %s, falling back to re-download: %s", user_id, exc)
            return False

        try:
            await self.bot.delete_message(chat_id, status_message_id)
        except TelegramError:
            pass
        return True

    async def _queued_heartbeat(self, job: Job) -> None:
        dots = ["", ".", "..", "..."]
        i = 0
        while job.task is None and not job.cancelled:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            if job.task is not None or job.cancelled:
                break
            i = (i + 1) % len(dots)
            await self._emit(job, f"Waiting for a free slot{dots[i]}", push=False, markup=QUEUED_MARKUP)

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job = await self._queue.get()

            if job.cancelled:
                ac.log_download(job.user_id, job.url, "cancelled")
                job.header = "Cancelled"
                await self._emit(job, "Cancelled before it started", markup=retry_menu())
                self._jobs_by_user.pop(job.user_id, None)
                self._queue.task_done()
                continue

            job.task = asyncio.current_task()
            try:
                await self._run_job(job)
                ac.log_download(job.user_id, job.url, "success")
            except asyncio.CancelledError:
                ac.log_download(job.user_id, job.url, "cancelled")
                job.header = "Cancelled"
                await self._emit(job, "Cancelled", markup=retry_menu())
            except NoToolSucceeded as exc:
                ac.log_download(job.user_id, job.url, "failed")
                log.exception("Job %s failed", job.job_id)
                job.header = "Failed"
                await self._emit(job, exc.primary_error, markup=retry_menu())
            except Exception as exc:  # noqa: BLE001
                ac.log_download(job.user_id, job.url, "failed")
                log.exception("Job %s failed", job.job_id)
                job.header = "Failed"
                await self._emit(job, str(exc)[:200], markup=retry_menu())
            finally:
                self._jobs_by_user.pop(job.user_id, None)
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        job.header = "Downloading"
        provider = _friendly_domain(job.url)
        await self._emit(job, f"Link: {provider}", markup=QUEUED_MARKUP)

        last_edit_time = 0.0
        last_percent = -100.0

        def progress_cb(tool_name: str, percent: float | None, speed: str | None, eta: str | None,
                        stage: str | None = None) -> None:
            nonlocal last_edit_time, last_percent
            push = stage is not None

            if not push:
                now = time.monotonic()
                if (now - last_edit_time) < MIN_EDIT_INTERVAL_SEC and abs((percent or 0) - last_percent) < MIN_PERCENT_DELTA:
                    return
                last_edit_time = now
                last_percent = percent or 0

            label = stage or TOOL_LABEL.get(tool_name, "Downloading")
            if percent is None:
                line = label
            else:
                bar = render_bar(percent)
                meta = []
                if speed:
                    meta.append(speed)
                if eta and eta not in ("~", ""):
                    meta.append(f"ETA {eta}")
                meta_str = f" · {' · '.join(meta)}" if meta else ""
                line = f"{label} <code>{bar}</code> {percent:.0f}%{meta_str}"

            asyncio.create_task(self._emit(job, line, push=push, markup=QUEUED_MARKUP))

        with job_workspace() as workspace:
            files = await dispatch_download(job.url, workspace, job.settings, job.user_id, progress_cb)

            for f in files:
                if f.suffix.lower() in VIDEO_EXTS:
                    await self._cache_video_file(job.user_id, job.url, f)

            job.header = "Sending"
            await self._emit(job, "Uploading to Telegram...", markup=QUEUED_MARKUP)
            await self._send_files(job, files)

    async def _send_with_ticker(self, job: Job, send_coro_factory, size_bytes: int):
        async def ticker() -> None:
            start = time.monotonic()
            mb = size_bytes / 1_000_000
            while True:
                elapsed = int(time.monotonic() - start)
                await self._emit(job, f"Sending {mb:.1f} MB • {elapsed}s elapsed", push=False, markup=QUEUED_MARKUP)
                await asyncio.sleep(SEND_TICKER_INTERVAL_SEC)

        ticker_task = asyncio.create_task(ticker())
        try:
            return await send_coro_factory()
        finally:
            ticker_task.cancel()

    async def _send_files(self, job: Job, files: list[Path]) -> None:
        for f in files:
            suffix = f.suffix.lower()
            size = f.stat().st_size

            if job.force_document:
                caption = messages.all_done_caption(f.stem[:100]) + "\n\nSent as a file — not re-compressed by Telegram."
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self._send_with_ticker(
                        job,
                        lambda: self.bot.send_document(
                            job.chat_id, input_file, caption=caption, parse_mode=ParseMode.HTML,
                            read_timeout=180, write_timeout=180, connect_timeout=60,
                        ),
                        size,
                    )
                continue

            caption = messages.all_done_caption(f.stem[:100])
            with open(f, "rb") as fh:
                input_file = InputFile(fh, filename=f.name)
                if suffix in VIDEO_EXTS:
                    video_caption = caption + "\n\nWant the original file instead of this compressed preview?"
                    await self._send_with_ticker(
                        job,
                        lambda: self.bot.send_video(
                            job.chat_id, input_file, caption=video_caption, parse_mode=ParseMode.HTML,
                            supports_streaming=True, reply_markup=send_as_file_menu(),
                            read_timeout=120, write_timeout=120, connect_timeout=60,
                        ),
                        size,
                    )
                elif suffix in {".mp3", ".m4a", ".opus", ".flac", ".wav"}:
                    await self.bot.send_audio(job.chat_id, input_file, caption=caption,
                                               parse_mode=ParseMode.HTML,
                                               read_timeout=120, write_timeout=120, connect_timeout=60)
                elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                    await self.bot.send_photo(job.chat_id, input_file, caption=caption,
                                               parse_mode=ParseMode.HTML)
                else:
                    await self.bot.send_document(job.chat_id, input_file, caption=caption,
                                                  parse_mode=ParseMode.HTML,
                                                  read_timeout=120, write_timeout=120, connect_timeout=60)
        try:
            await self.bot.delete_message(job.chat_id, job.status_message_id)
        except TelegramError:
            pass

    async def _safe_edit(self, job: Job, text: str, markup: InlineKeyboardMarkup | None = None,
                          clear_markup: bool = False) -> None:
        reply_markup = None if clear_markup else markup
        try:
            if job.is_photo:
                await self.bot.edit_message_caption(
                    chat_id=job.chat_id, message_id=job.status_message_id,
                    caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
            else:
                await self.bot.edit_message_text(
                    text, chat_id=job.chat_id, message_id=job.status_message_id,
                    parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
        except TelegramError as exc:
            log.debug("Edit skipped for job %s: %s", job.job_id, exc)
