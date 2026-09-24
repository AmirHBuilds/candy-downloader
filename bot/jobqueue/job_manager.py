"""
CandyDownloader's job manager.

Every incoming link becomes a Job, identified by its own request token
(rid) - NOT just the user's id. Jobs go into an asyncio.Queue; a fixed
pool of worker tasks pulls them off and processes them, so:

  - the bot's message handler returns instantly (never blocks on a download)
  - at most MAX_CONCURRENT_DOWNLOADS run at once, protecting the VPS
  - a second link sent before the first resolves can never clobber the
    first one's state - each has its own token, carried through from the
    quick-pick buttons all the way to completion

STEP LOG: instead of one static line, the status message shows a small
rolling log of the last 3 real events (which tool is being tried,
postprocessing stages, etc.) - each genuine transition pushes a new
line; a bare percent tick updates the current line in place so the bar
animates smoothly without spamming new lines or leaving stale duplicate
lines behind.
"""
import asyncio
import logging
import re
import shutil
import time
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
from ui.quick_menu import queued_menu, send_as_file_menu, retry_menu, cancelled_menu
from utils.cleanup import job_workspace, new_cache_path

log = logging.getLogger("candy.jobs")

MIN_EDIT_INTERVAL_SEC = 1.5
MIN_PERCENT_DELTA = 3.0
HEARTBEAT_INTERVAL_SEC = 2.5
RECENT_FILE_TTL_SECONDS = 5 * 60
MAX_STEP_LINES = 3

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm"}

_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_ERROR_PREFIX_RE = re.compile(r"(?i)\berror:\s*")
_ID_COLON_RE = re.compile(r"\b(?=[\w-]*\d)[\w-]{6,}:\s*")
_BOILERPLATE_CUTS = (
    "; please report", "please report this issue",
    "Confirm you are on the latest version",
)
_UNSUPPORTED_MARKERS = ("unsupported url", "produced no files", "not a downloadable file", "no extractor")


def _sanitize_step(text: str, url: str) -> str:
    cleaned = (text or "").replace(url, "this link")
    for cut in _BOILERPLATE_CUTS:
        idx = cleaned.find(cut)
        if idx != -1:
            cleaned = cleaned[:idx]
    cleaned = _ERROR_PREFIX_RE.sub("", cleaned)
    cleaned = _BRACKET_RE.sub("", cleaned)
    cleaned = _ID_COLON_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .;")
    if len(cleaned) > 140:
        cleaned = cleaned[:137].rstrip() + "..."
    return cleaned or "failed"


def _friendly_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host or "this link"
    except Exception:  # noqa: BLE001
        return "this link"


@dataclass
class Job:
    rid: str
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
        self._jobs_by_rid: dict[str, Job] = {}
        self._recent_files: dict[str, dict] = {}

    def start(self) -> None:
        for i in range(self._max_concurrent):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))
        log.info("Started %d download workers", self._max_concurrent)

    async def enqueue(self, rid: str, user_id: int, chat_id: int, url: str, settings: dict,
                       status_message_id: int, is_photo: bool = False,
                       force_document: bool = False) -> Job:
        job = Job(
            rid=rid,
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            settings=settings,
            status_message_id=status_message_id,
            is_photo=is_photo,
            force_document=force_document,
            header="Queued",
        )
        self._jobs_by_rid[rid] = job
        await self._queue.put(job)
        asyncio.create_task(self._queued_heartbeat(job))
        return job

    def cancel(self, rid: str, user_id: int) -> bool:
        job = self._jobs_by_rid.get(rid)
        if not job or job.cancelled or job.user_id != user_id:
            return False
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
        return True

    def cancel_all_for_user(self, user_id: int) -> int:
        """Used by the plain /cancel command, which has no specific rid
        in mind - stops every job currently running or queued for that
        user. Returns how many were cancelled."""
        count = 0
        for job in list(self._jobs_by_rid.values()):
            if job.user_id == user_id and not job.cancelled:
                job.cancelled = True
                if job.task and not job.task.done():
                    job.task.cancel()
                count += 1
        return count

    def active_summary(self, user_id: int | None = None) -> str:
        jobs = [j for j in self._jobs_by_rid.values() if user_id is None or j.user_id == user_id]
        if not jobs:
            return "Nothing in the queue right now."
        lines = [f"{OWNER_EMOJI} <b>Current queue</b>"]
        for job in jobs:
            state = "running" if job.task and not job.task.done() else "waiting"
            lines.append(f"• <code>{job.url[:40]}</code> — {state}")
        return "\n".join(lines)

    def _render(self, job: Job) -> str:
        lines = [f"{OWNER_EMOJI} <b>{job.header}</b>"]
        if job.steps:
            recent = job.steps[-MAX_STEP_LINES:]
            rendered = []
            for i, s in enumerate(recent):
                is_current = i == len(recent) - 1
                text = f"<b>{s}</b>" if (is_current and "<" not in s) else s
                rendered.append(f"• {text}")
            lines.append("\n".join(rendered))
        lines.append(f"<code>{job.url}</code>")
        return "\n\n".join(lines)

    async def _emit(self, job: Job, text: str, push: bool = True,
                     markup: InlineKeyboardMarkup | None = None, sanitize: bool = True) -> None:
        if push and sanitize:
            text = _sanitize_step(text, job.url)
        if push or not job.steps:
            job.steps.append(text)
            if len(job.steps) > MAX_STEP_LINES:
                job.steps.pop(0)
        else:
            job.steps[-1] = text
        await self._safe_edit(job, self._render(job), markup=markup)

    async def _cache_video_file(self, rid: str, url: str, src: Path) -> None:
        old = self._recent_files.pop(rid, None)
        if old:
            old["path"].unlink(missing_ok=True)

        cached_path = new_cache_path(src.suffix)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, shutil.copy, src, cached_path)
        except OSError as exc:
            log.warning("Could not cache %s -> %s for reuse: %s", src, cached_path, exc)
            return

        self._recent_files[rid] = {"path": cached_path, "url": url, "title": src.stem[:100]}
        log.info("Cached %s for rid %s (url=%s)", cached_path, rid, url)
        asyncio.create_task(self._expire_cached_file(rid, cached_path))

    async def _expire_cached_file(self, rid: str, path: Path) -> None:
        await asyncio.sleep(RECENT_FILE_TTL_SECONDS)
        entry = self._recent_files.get(rid)
        if entry and entry["path"] == path:
            self._recent_files.pop(rid, None)
        path.unlink(missing_ok=True)

    def has_cached_video(self, rid: str, url: str) -> bool:
        entry = self._recent_files.get(rid)
        hit = bool(entry and entry["url"] == url and entry["path"].exists())
        if entry and not hit:
            log.info("Cache miss for rid %s: have url=%r, wanted url=%r", rid, entry.get("url"), url)
        return hit

    async def send_cached_as_document(self, rid: str, chat_id: int, url: str,
                                       status_message_id: int) -> bool:
        entry = self._recent_files.get(rid)
        if not entry or entry["url"] != url or not entry["path"].exists():
            return False

        path = entry["path"]
        title = entry.get("title") or path.stem
        caption = messages.all_done_caption(title) + "\n\nSent as a file — not re-compressed by Telegram."
        try:
            with open(path, "rb") as fh:
                input_file = InputFile(fh, filename=f"{title}{path.suffix}")
                await self.bot.send_document(chat_id, input_file, caption=caption, parse_mode=ParseMode.HTML,
                                              read_timeout=180, write_timeout=180, connect_timeout=60)
        except (OSError, TelegramError) as exc:
            log.warning("Cached send failed for rid %s, falling back to re-download: %s", rid, exc)
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
            await self._emit(job, f"Waiting for a free slot{dots[i]}", push=False,
                              markup=queued_menu(job.rid))

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job = await self._queue.get()

            if job.cancelled:
                ac.log_download(job.user_id, job.url, "cancelled")
                job.header = "Cancelled"
                await self._emit(job, "Cancelled before it started", markup=cancelled_menu(job.rid))
                self._jobs_by_rid.pop(job.rid, None)
                self._queue.task_done()
                continue

            job.task = asyncio.current_task()
            try:
                await self._run_job(job)
                ac.log_download(job.user_id, job.url, "success")
            except asyncio.CancelledError:
                ac.log_download(job.user_id, job.url, "cancelled")
                job.header = "Cancelled"
                await self._emit(job, "Cancelled", markup=cancelled_menu(job.rid))
            except NoToolSucceeded as exc:
                ac.log_download(job.user_id, job.url, "failed")
                log.exception("Job %s failed", job.rid)
                job.header = "Failed"
                if all(any(m in v.lower() for m in _UNSUPPORTED_MARKERS) for v in exc.attempts.values()):
                    await self._emit(job, "None of our downloaders support this link.",
                                      markup=retry_menu(job.rid), sanitize=False)
                else:
                    cleaned = _sanitize_step(exc.primary_error, job.url)
                    await self._emit(job, messages.generic_error(cleaned), markup=retry_menu(job.rid),
                                      sanitize=False)
            except Exception as exc:  # noqa: BLE001
                ac.log_download(job.user_id, job.url, "failed")
                log.exception("Job %s failed", job.rid)
                job.header = "Failed"
                cleaned = _sanitize_step(str(exc), job.url)
                await self._emit(job, messages.generic_error(cleaned), markup=retry_menu(job.rid), sanitize=False)
            finally:
                self._jobs_by_rid.pop(job.rid, None)
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        job.header = "Downloading"
        provider = _friendly_domain(job.url)
        await self._emit(job, f"Link: {provider}", markup=queued_menu(job.rid))

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

            if percent is None:
                meta = []
                if speed:
                    meta.append(speed)
                if eta and eta not in ("~", ""):
                    meta.append(f"ETA {eta}")
                line = f"{stage} · {' · '.join(meta)}" if (stage and meta) else (stage or "Working...")
            else:
                bar = render_bar(percent)
                meta = []
                if speed:
                    meta.append(speed)
                if eta and eta not in ("~", ""):
                    meta.append(f"ETA {eta}")
                meta_str = f" · {' · '.join(meta)}" if meta else ""
                prefix = f"{stage} " if stage else ""
                line = f"{prefix}<code>{bar}</code> {percent:.0f}%{meta_str}"

            asyncio.create_task(self._emit(job, line, push=push, markup=queued_menu(job.rid)))

        with job_workspace() as workspace:
            files = await dispatch_download(job.url, workspace, job.settings, job.user_id, progress_cb)

            for f in files:
                if f.suffix.lower() in VIDEO_EXTS:
                    await self._cache_video_file(job.rid, job.url, f)

            job.header = "Sending"
            await self._emit(job, "Uploading to Telegram...", markup=queued_menu(job.rid))
            await self._send_files(job, files)

    async def _send_files(self, job: Job, files: list[Path]) -> None:
        for f in files:
            suffix = f.suffix.lower()
            size_mb = f.stat().st_size / 1_000_000

            if job.force_document:
                caption = messages.all_done_caption(f.stem[:100]) + "\n\nSent as a file — not re-compressed by Telegram."
                await self._emit(job, f"Sending {size_mb:.1f} MB...", markup=None)
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_document(
                        job.chat_id, input_file, caption=caption, parse_mode=ParseMode.HTML,
                        read_timeout=180, write_timeout=180, connect_timeout=60,
                    )
                continue

            caption = messages.all_done_caption(f.stem[:100])
            if suffix in VIDEO_EXTS:
                await self._emit(job, f"Sending {size_mb:.1f} MB...", markup=None)
                video_caption = caption + "\n\nWant the original file instead of this compressed preview?"
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_video(
                        job.chat_id, input_file, caption=video_caption, parse_mode=ParseMode.HTML,
                        supports_streaming=True, reply_markup=send_as_file_menu(job.rid),
                        read_timeout=120, write_timeout=120, connect_timeout=60,
                    )
            elif suffix in {".mp3", ".m4a", ".opus", ".flac", ".wav"}:
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_audio(job.chat_id, input_file, caption=caption,
                                               parse_mode=ParseMode.HTML,
                                               read_timeout=120, write_timeout=120, connect_timeout=60)
            elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_photo(job.chat_id, input_file, caption=caption,
                                               parse_mode=ParseMode.HTML)
            else:
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_document(job.chat_id, input_file, caption=caption,
                                                  parse_mode=ParseMode.HTML,
                                                  read_timeout=120, write_timeout=120, connect_timeout=60)
        try:
            await self.bot.delete_message(job.chat_id, job.status_message_id)
        except TelegramError:
            pass

    async def _safe_edit(self, job: Job, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            if job.is_photo:
                await self.bot.edit_message_caption(
                    chat_id=job.chat_id, message_id=job.status_message_id,
                    caption=text, parse_mode=ParseMode.HTML, reply_markup=markup,
                )
            else:
                await self.bot.edit_message_text(
                    text, chat_id=job.chat_id, message_id=job.status_message_id,
                    parse_mode=ParseMode.HTML, reply_markup=markup,
                )
        except TelegramError as exc:
            log.debug("Edit skipped for job %s: %s", job.rid, exc)
