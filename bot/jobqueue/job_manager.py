"""
CandyDownloader's job manager.

Every incoming link becomes a Job and goes into an asyncio.Queue. A fixed
pool of worker tasks pulls jobs off the queue and processes them, so:

  - the bot's message handler returns instantly (never blocks on a download)
  - at most MAX_CONCURRENT_DOWNLOADS run at once, protecting the VPS
  - progress-bar edits happen on their own throttled schedule and never
    stall the download itself (editing Telegram messages is just another
    async call scheduled alongside the download, not interleaved with it)

The status message can be plain text or a photo-with-caption (when we
showed a video thumbnail during the quick-pick step) - _safe_edit picks
the right Telegram method for whichever it is. The link stays visible in
every state (queued, downloading, done) so it's always there to copy.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from telegram import Bot, InputFile, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import OWNER_EMOJI
from downloader.dispatcher import download as dispatch_download, NoToolSucceeded
from settings import access_control as ac
from ui import messages
from ui.progress import render_status
from ui.quick_menu import queued_menu, send_as_file_menu, retry_menu
from utils.cleanup import job_workspace

log = logging.getLogger("candy.jobs")

MIN_EDIT_INTERVAL_SEC = 1.5   # don't hammer Telegram's edit_message rate limit
MIN_PERCENT_DELTA = 3.0
HEARTBEAT_INTERVAL_SEC = 2.5

TOOL_EMOJI = {"ytdlp": "▸", "gallerydl": "▸", "generic": "▸", "spotify": "♪"}
TOOL_LABEL = {"ytdlp": "Downloading video", "gallerydl": "Downloading gallery",
              "generic": "Downloading file", "spotify": "Finding & downloading track"}

QUEUED_MARKUP = queued_menu()


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
    task: Optional[asyncio.Task] = field(default=None)
    cancelled: bool = False


class JobManager:
    def __init__(self, bot: Bot, max_concurrent: int):
        self.bot = bot
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._max_concurrent = max_concurrent
        self._workers: list[asyncio.Task] = []
        self._jobs_by_user: dict[int, Job] = {}

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
        )
        self._jobs_by_user[user_id] = job
        await self._queue.put(job)
        asyncio.create_task(self._queued_heartbeat(job))
        return job

    def cancel_for_user(self, user_id: int) -> bool:
        """Works whether the job is still waiting in line or already
        running - either way it's marked cancelled and, if a worker has
        already picked it up, the running task is interrupted too."""
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

    async def _queued_heartbeat(self, job: Job) -> None:
        """Gives a visible sign of life while a job sits in the queue,
        before a worker has picked it up - otherwise "Queued" just sits
        there looking frozen if MAX_CONCURRENT_DOWNLOADS is saturated."""
        dots = ["", ".", "..", "..."]
        i = 0
        while job.task is None and not job.cancelled:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
            if job.task is not None or job.cancelled:
                break
            i = (i + 1) % len(dots)
            text = messages.with_link(f"{messages.QUEUED}{dots[i]}", job.url)
            await self._safe_edit(job, text, markup=QUEUED_MARKUP)

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job = await self._queue.get()

            if job.cancelled:
                # cancelled while still waiting in line - never actually ran
                ac.log_download(job.user_id, job.url, "cancelled")
                await self._safe_edit(job, messages.with_link(messages.CANCELLED, job.url), clear_markup=True)
                self._jobs_by_user.pop(job.user_id, None)
                self._queue.task_done()
                continue

            job.task = asyncio.current_task()
            try:
                await self._run_job(job)
                ac.log_download(job.user_id, job.url, "success")
            except asyncio.CancelledError:
                ac.log_download(job.user_id, job.url, "cancelled")
                await self._safe_edit(job, messages.with_link(messages.CANCELLED, job.url), clear_markup=True)
            except NoToolSucceeded as exc:
                ac.log_download(job.user_id, job.url, "failed")
                log.exception("Job %s failed", job.job_id)
                text = messages.with_link(messages.generic_error(exc.primary_error), job.url)
                await self._safe_edit(job, text, markup=retry_menu())
            except Exception as exc:  # noqa: BLE001
                ac.log_download(job.user_id, job.url, "failed")
                log.exception("Job %s failed", job.job_id)
                text = messages.with_link(messages.generic_error(str(exc)), job.url)
                await self._safe_edit(job, text, markup=retry_menu())
            finally:
                self._jobs_by_user.pop(job.user_id, None)
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        last_edit_time = 0.0
        last_percent = -100.0

        await self._safe_edit(
            job, messages.with_link("Preparing your download…", job.url), markup=QUEUED_MARKUP,
        )

        def progress_cb(tool_name: str, percent: float, speed: str | None, eta: str | None) -> None:
            nonlocal last_edit_time, last_percent
            now = time.monotonic()
            if (now - last_edit_time) < MIN_EDIT_INTERVAL_SEC and abs(percent - last_percent) < MIN_PERCENT_DELTA:
                return
            last_edit_time = now
            last_percent = percent
            status = render_status(
                TOOL_EMOJI.get(tool_name, "▸"),
                TOOL_LABEL.get(tool_name, "Downloading"),
                percent, speed, eta,
            )
            text = messages.with_link(status, job.url)
            asyncio.create_task(self._safe_edit(job, text, markup=QUEUED_MARKUP))

        with job_workspace() as workspace:
            files = await dispatch_download(job.url, workspace, job.settings, job.user_id, progress_cb)
            await self._safe_edit(job, messages.with_link(messages.UPLOADING, job.url), clear_markup=True)
            await self._send_files(job, files)

    async def _send_files(self, job: Job, files: list[Path]) -> None:
        for f in files:
            suffix = f.suffix.lower()

            if job.force_document:
                caption = messages.all_done_caption(f.stem[:100]) + "\n\nOriginal file — not re-compressed."
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_document(job.chat_id, input_file, caption=caption,
                                                  parse_mode=ParseMode.HTML,
                                                  read_timeout=180, write_timeout=180, connect_timeout=60)
                continue

            caption = messages.all_done_caption(f.stem[:100])
            with open(f, "rb") as fh:
                input_file = InputFile(fh, filename=f.name)
                if suffix in {".mp4", ".mkv", ".mov", ".webm"}:
                    # Attach the "send as file" offer right on this same
                    # message - no separate follow-up message needed.
                    video_caption = caption + "\n\nWant the original file instead of this compressed preview?"
                    await self.bot.send_video(job.chat_id, input_file, caption=video_caption,
                                               parse_mode=ParseMode.HTML, supports_streaming=True,
                                               reply_markup=send_as_file_menu(),
                                               read_timeout=120, write_timeout=120, connect_timeout=60)
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
            # editing fails if text is identical or message was deleted - both harmless
            log.debug("Edit skipped for job %s: %s", job.job_id, exc)
