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

from telegram import Bot, InputFile, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import OWNER_EMOJI
from ui.steplog import step_line
from downloader import tools as media_tools
from ui import tools_menu
from downloader import cookie_health
from downloader.dispatcher import download as dispatch_download, NoToolSucceeded
from downloader.errors import JobCancelled
from settings import access_control as ac
from ui import messages
from ui.progress import render_progress, resolve_style
from ui.quick_menu import queued_menu, send_as_file_menu, sent_menu, retry_menu, cancelled_menu
from utils.cleanup import job_workspace, new_cache_path
from utils.text import esc, sanitize_step as _sanitize_step, site_label

log = logging.getLogger("candy.jobs")

MIN_EDIT_INTERVAL_SEC = 1.5
MIN_PERCENT_DELTA = 3.0
HEARTBEAT_INTERVAL_SEC = 2.5
RECENT_FILE_TTL_SECONDS = 5 * 60
MAX_STEP_LINES = 5   # room for: tool line + clip line + Video line + Audio line + a postprocessing stage

# Extensions that are never the actual media - sidecar/thumbnail files
# that can end up in the workspace alongside the real output. Everything
# ELSE produced by a video-mode job is treated as the media to cache,
# regardless of container (.mp4/.mkv/.webm/.ts/...): hardcoding a fixed
# list of "video" extensions here previously meant that whenever yt-dlp
# picked a container we hadn't enumerated (e.g. falling back to mkv
# because a height-capped format paired VP9 video with Opus audio - a
# combination that can't remux cleanly into mp4), the file silently never
# got cached, so "Send as file instead" would find no cache and quietly
# have to fall through to a slower re-download instead of behaving
# identically to the max-quality case.
_SUBTITLE_EXTS = {".srt", ".vtt", ".ass"}
# Never a title for /history and never kept for the "send as file" resend (subtitle files included:
# they are sent as documents right after the video, see _send_files).
_NON_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".part", ".ytdl", ".json", ".txt"} | _SUBTITLE_EXTS
_LANG_SUFFIX = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*$")
_UNSUPPORTED_MARKERS = ("unsupported url", "produced no files", "not a downloadable file", "no extractor")
# Containers Telegram will actually render as an inline, scrubbable video
# preview via sendVideo. Anything else we get (e.g. yt-dlp falling back to
# mkv because a capped height forced a VP9+Opus pairing) goes straight out
# as a document instead of a preview that likely wouldn't play anyway.
_STREAMABLE_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm"}


def _plain_reason(text: str) -> str:
    """First line of an error, short, as plain text (the batch summary escapes it)."""
    text = (text or "").strip()
    return text.splitlines()[0][:100] if text else ""


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
    # For /history. Seeded from the preview's title when we had one (so failed
    # and cancelled jobs still get a name); replaced by the delivered file's
    # name on success, which works for every tool, ADHD Mode included.
    title: str = ""
    steps: list[str] = field(default_factory=list)
    # Permanent header facts (type / size / site) shown ABOVE the scrolling
    # steps and updated in place as more becomes known - unlike steps they
    # never scroll away. Keys: type, size (bytes), site.
    info: dict = field(default_factory=dict)
    header: str = "Working"
    task: Optional[asyncio.Task] = field(default=None)
    cancelled: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set once the job has reached its final state (done / failed / cancelled).
    # Progress that is still in flight after that is dropped, so a stale
    # "One moment…" can't overwrite the final message.
    terminal: bool = False
    # Part of a Batch (playlist / several links)? Then the batch owns the status
    # message and these jobs only report into it (see jobqueue/batch.py).
    batch: Optional[object] = field(default=None, repr=False)
    percent: Optional[float] = None       # latest progress, for the batch's shared message
    outcome: str = ""                     # "success" | "failed" | "cancelled" once it has finished
    error_text: str = ""                  # plain one-line reason when it failed
    # Status-message edit pump (see _safe_edit): the newest text waiting to be
    # sent, and whether a sender is already running.
    pending_edit: Optional[tuple] = field(default=None, repr=False)
    editing: bool = field(default=False, repr=False)


class JobManager:
    def __init__(self, bot: Bot, max_concurrent: int):
        self.bot = bot
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._max_concurrent = max_concurrent
        self._workers: list[asyncio.Task] = []
        self._jobs_by_rid: dict[str, Job] = {}
        # rid -> every file of that job, in delivery order (a playlist or a
        # multi-clip job has several; "Send as file instead" re-sends all of them)
        self._recent_files: dict[str, list[dict]] = {}

    def start(self) -> None:
        for i in range(self._max_concurrent):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))
        log.info("Started %d download workers", self._max_concurrent)

    async def enqueue(self, rid: str, user_id: int, chat_id: int, url: str, settings: dict,
                       status_message_id: int, is_photo: bool = False,
                       force_document: bool = False, title: str = "", batch: object = None) -> Job:
        job = Job(
            rid=rid,
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            settings=settings,
            status_message_id=status_message_id,
            is_photo=is_photo,
            force_document=force_document,
            title=title,
            header="Queued",
            batch=batch,
        )
        self._jobs_by_rid[rid] = job
        await self._queue.put(job)
        if batch is None:      # a batch has its own shared message; "waiting" is a count there
            asyncio.create_task(self._queued_heartbeat(job))
        log.info("Enqueued job rid=%s force_document=%s quality=%s mode=%s",
                 rid, force_document, settings.get("quality"), settings.get("mode"))
        return job

    def active_count(self) -> int:
        """Jobs that are queued or running right now."""
        return sum(1 for job in self._jobs_by_rid.values() if not job.terminal)

    def is_active(self, rid: str) -> bool:
        """A job with this id is queued or running (the toolbox must not start a second one on the same file)."""
        job = self._jobs_by_rid.get(rid)
        return bool(job and not job.terminal)

    def cancel(self, rid: str, user_id: int) -> bool:
        job = self._jobs_by_rid.get(rid)
        if not job or job.cancelled or job.user_id != user_id:
            return False
        job.cancelled = True
        job.cancel_event.set()
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
                job.cancel_event.set()
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
            lines.append(f"• <code>{esc(job.url[:40])}</code> — {state}")
        return "\n".join(lines)

    def _render(self, job: Job) -> str:
        if job.settings.get("adhd_mode"):
            return self._render_adhd(job)
        lines = [f"{OWNER_EMOJI} <b>{job.header}</b>"]
        log_lines, notices = [], []
        for step in job.steps[-MAX_STEP_LINES:]:
            # A step containing '<' is ready-made HTML (the failure message,
            # with its <code> block); ordinary steps are escaped and never do.
            # Telegram can't nest formatting inside <pre>, so those go below it.
            (notices if "<" in step else log_lines).append(step)
        if log_lines:
            lines.append("<pre>" + "\n".join(step_line(s) for s in log_lines) + "</pre>")
        lines.extend(notices)
        info_line = self._info_line(job)
        if info_line:
            lines.append(info_line)
        return "\n\n".join(lines)

    @staticmethod
    def _history_quality(settings: dict) -> str:
        """What /history shows in the quality column: the audio format for
        an audio job (there's no "quality" concept there, just a codec), or
        the picked video quality otherwise."""
        base = settings.get("audio_format", "audio") if settings.get("mode") == "audio" \
            else settings.get("quality", "best")
        sections = settings.get("sections")
        return f"{base} ✄{len(sections)}" if sections else base

    def _log_history(self, job: Job, status: str) -> None:
        job.outcome = status
        if job.settings.get("tool"):
            return                      # toolbox work on a file the person sent: not a download, nothing to link to
        ac.log_download(job.user_id, job.url, status, job.settings.get("mode", ""),
                        self._history_quality(job.settings), job.title)

    # One place to change the icons on the info line.
    _TYPE_ICONS = {"Audio": "🎵", "Video": "🎬", "File": "📄", "Tool": "🧰"}

    def _info_line(self, job: Job) -> str:
        """'• 🎵 Audio | 📦 230 MB | 🔗 Youtube' - parts appear as they become known."""
        parts = []
        kind = job.info.get("type")
        if kind:
            parts.append(f"{self._TYPE_ICONS.get(kind, '')} {kind}".strip())
        sections = job.settings.get("sections")
        if sections:
            n = len(sections)
            merged = " merged" if job.settings.get("sections_merge") and n > 1 else ""
            parts.append(f"✂️ {n} clip{'s' if n != 1 else ''}{merged}")   # emoji form, like 🎬 / 🔗 on this line
        size = job.info.get("size")
        if size:
            mb = size / 1_000_000
            parts.append(f"📦 {mb / 1000:.1f} GB" if mb >= 1000 else f"📦 {mb:.0f} MB")
        if job.info.get("site"):
            parts.append(f"🔗 {esc(job.info['site'])}")
        return ("↳ " + " | ".join(parts)) if parts else ""

    async def _emit_progress(self, job: Job, text: str, push: bool, markup: InlineKeyboardMarkup | None) -> None:
        """A progress update. Checked when it RUNS, not when it was scheduled:
        these are fire-and-forget tasks, and one can start after the job has
        already finished."""
        if not job.terminal:
            await self._emit(job, text, push=push, markup=markup)

    async def _refresh(self, job: Job) -> None:
        """Re-render without touching the steps - used when only the info line changed."""
        if job.terminal:
            return
        if job.batch is not None:
            job.batch.note(job)
            await job.batch.refresh(self)
            return
        await self._safe_edit(job, self._render(job), markup=queued_menu(job.rid, job.url))

    _ADHD_HEADERS = {
        "Queued": "🍬 On it...",
        "Downloading": "🍬 Grabbing your video...",
        "Sending": "🍬 Almost there...",
        "Cancelled": "✕ Cancelled",
        "Failed": "✕ Didn't work",
    }

    def _render_adhd(self, job: Job) -> str:
        """ADHD Mode's whole point is no clutter: one friendly line, one
        bar, nothing technical (no tool names, no "Trying gallery-dl...",
        no speed/ETA breakdown) - the step-by-step log above is exactly
        what this mode exists to skip."""
        header = self._ADHD_HEADERS.get(job.header, f"🍬 {job.header}")
        body = job.steps[-1] if job.steps else "Starting…"
        return f"<b>{header}</b>\n\n{body}"

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
        if job.batch is not None:
            job.batch.note(job)
            await job.batch.refresh(self)
            return
        await self._safe_edit(job, self._render(job), markup=markup)

    def _drop_cached(self, rid: str) -> None:
        """Forget (and delete) everything cached for this job."""
        for entry in self._recent_files.pop(rid, []):
            entry["path"].unlink(missing_ok=True)

    async def _cache_video_file(self, rid: str, url: str, src: Path) -> None:
        """Add one delivered file to this job's cache. Callers clear the job's
        old entries first (_drop_cached) - a per-call reset here would keep
        only the last file of a playlist / multi-clip job."""
        cached_path = new_cache_path(src.suffix)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, shutil.copy, src, cached_path)
        except OSError as exc:
            log.warning("Could not cache %s -> %s for reuse: %s", src, cached_path, exc)
            return

        self._recent_files.setdefault(rid, []).append(
            {"path": cached_path, "url": url, "title": src.stem[:100], "name": src.name})
        log.info("Cached %s for rid %s (url=%s)", cached_path, rid, url)
        asyncio.create_task(self._expire_cached_file(rid, cached_path))

    async def _expire_cached_file(self, rid: str, path: Path) -> None:
        await asyncio.sleep(RECENT_FILE_TTL_SECONDS)
        entries = self._recent_files.get(rid, [])
        remaining = [e for e in entries if e["path"] != path]
        if remaining:
            self._recent_files[rid] = remaining
        else:
            self._recent_files.pop(rid, None)
        path.unlink(missing_ok=True)

    def has_cached_video(self, rid: str, url: str) -> bool:
        entries = self._recent_files.get(rid)
        hit = bool(entries) and all(e["url"] == url and e["path"].exists() for e in entries)
        if not entries:
            log.info("No cache entry at all for rid %s (wanted url=%r)", rid, url)
        elif not hit:
            log.info("Cache miss for rid %s: wanted url=%r, have %s", rid, url,
                      [(e.get("url"), e["path"].exists()) for e in entries])
        return hit

    async def send_cached_as_document(self, rid: str, chat_id: int, url: str,
                                       status_message_id: int) -> bool:
        """Re-send every cached file of this job as a plain document. Returns
        False (so the caller re-downloads) only if NOTHING could be sent; once
        at least one file went out, a failure stops here rather than falling
        back to a re-download that would deliver the earlier ones twice."""
        if not self.has_cached_video(rid, url):
            return False

        entries = list(self._recent_files[rid])
        sent_any = False
        for index, entry in enumerate(entries):
            path = entry["path"]
            title = entry.get("title") or path.stem
            caption = messages.all_done_caption(title) + "\n\nSent as a file — not re-compressed by Telegram."
            is_last = index == len(entries) - 1
            try:
                with open(path, "rb") as fh:
                    input_file = InputFile(fh, filename=entry.get("name") or f"{title}{path.suffix}")
                    await self.bot.send_document(chat_id, input_file, caption=caption, parse_mode=ParseMode.HTML,
                                                  reply_markup=sent_menu(url) if is_last else None,
                                                  read_timeout=180, write_timeout=180, connect_timeout=60)
            except (OSError, TelegramError) as exc:
                log.warning("Cached send failed for rid %s (file %d of %d): %s", rid, index + 1, len(entries), exc)
                if not sent_any:
                    return False
                break
            sent_any = True
            log.info("Sent cached document for rid %s (%s)", rid, path)

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
                              markup=queued_menu(job.rid, job.url))

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            job = await self._queue.get()

            if job.cancelled:
                self._log_history(job, "cancelled")
                job.header = "Cancelled"
                await self._emit(job, "Cancelled before it started", markup=cancelled_menu(job.rid, job.url))
                self._jobs_by_rid.pop(job.rid, None)
                self._queue.task_done()
                await self._tell_batch(job)
                continue

            job.task = asyncio.current_task()
            try:
                await self._run_job(job)
                self._log_history(job, "success")
                if not job.settings.get("tool") and cookie_health.cookies_in_use(job.user_id, job.settings, job.url):
                    cookie_health.watch.record_success(job.user_id)
            except (asyncio.CancelledError, JobCancelled):
                job.terminal = True
                self._log_history(job, "cancelled")
                job.header = "Cancelled"
                await self._emit(job, "Cancelled", markup=cancelled_menu(job.rid, job.url))
            except NoToolSucceeded as exc:
                job.terminal = True
                self._log_history(job, "failed")
                log.exception("Job %s failed", job.rid)
                job.header = "Failed"
                job.error_text = _plain_reason(exc.primary_error)
                if all(any(m in v.lower() for m in _UNSUPPORTED_MARKERS) for v in exc.attempts.values()):
                    await self._emit(job, "None of our downloaders support this link.",
                                      markup=retry_menu(job.rid, job.url), sanitize=False)
                else:
                    cleaned = _sanitize_step(exc.primary_error, job.url)
                    await self._emit(job, messages.generic_error(cleaned), markup=retry_menu(job.rid, job.url),
                                      sanitize=False)
                await self._warn_if_cookies_died(job, " ".join([exc.primary_error, *exc.attempts.values()]))
            except Exception as exc:  # noqa: BLE001
                job.terminal = True
                self._log_history(job, "failed")
                log.exception("Job %s failed", job.rid)
                job.header = "Failed"
                job.error_text = _plain_reason(str(exc))
                cleaned = _sanitize_step(str(exc), job.url)
                await self._emit(job, messages.generic_error(cleaned), markup=retry_menu(job.rid, job.url), sanitize=False)
                await self._warn_if_cookies_died(job, str(exc))
            finally:
                job.terminal = True
                self._jobs_by_rid.pop(job.rid, None)
                self._queue.task_done()
                await self._tell_batch(job)

    async def _tell_batch(self, job: Job) -> None:
        """A job that belongs to a batch reports its outcome there. Never lets a
        problem in the batch's message stop the worker loop."""
        if job.batch is None:
            return
        try:
            await job.batch.job_finished(self, job)
        except Exception:  # noqa: BLE001
            log.exception("Batch update failed for job %s", job.rid)

    async def _warn_if_cookies_died(self, job: Job, error_text: str) -> None:
        """After a failed download: if it looks like a sign-in problem with the
        cookies this person has set up, tell them - once, in a separate
        message so it isn't lost when the status message changes."""
        if not cookie_health.cookies_in_use(job.user_id, job.settings, job.url):
            return
        if not cookie_health.watch.record_failure(job.user_id, error_text):
            return
        try:
            await self.bot.send_message(job.chat_id, cookie_health.COOKIE_ALERT, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            log.warning("Could not send the cookie warning to %s: %s", job.user_id, exc)

    async def _run_job(self, job: Job) -> None:
        job.header = "Working" if job.settings.get("tool") else "Downloading"
        adhd = job.settings.get("adhd_mode", False)
        bar_style = resolve_style(job.settings.get("bar_style"), adhd)
        if adhd:
            await self._emit(job, "Fetching…", markup=queued_menu(job.rid, job.url))
        else:
            if job.settings.get("tool"):
                job.info = {"type": "Tool"}
            else:
                job.info = {
                    "type": "Audio" if job.settings.get("mode") == "audio" else "Video",
                    "site": site_label(job.url),
                }
            await self._emit(job, "Starting…", markup=queued_menu(job.rid, job.url))

        last_edit_time = 0.0
        last_percent = -100.0

        current_label: str | None = None

        def progress_cb(tool_name: str, percent: float | None, speed: str | None, eta: str | None,
                        stage: str | None = None, label: str | None = None,
                        size: int | None = None) -> None:
            nonlocal last_edit_time, last_percent, current_label

            # Anything that isn't a video/audio stream download (gallery-dl,
            # plain file links) shouldn't be announced as "Video".
            if tool_name in ("gallerydl", "generic") and job.info and job.info.get("type") != "File":
                job.info["type"] = "File"

            if job.terminal:
                return

            if percent is not None:
                job.percent = percent

            if size is not None:
                job.info["size"] = size
                if percent is None and stage is None and label is None:
                    if not adhd:
                        asyncio.create_task(self._refresh(job))
                    return

            # yt-dlp downloads video and audio as two streams, each 0-100%.
            # A new label = a new stream = its own line, so the finished
            # "Video - 100%" line stays put while "Audio" progresses below it.
            new_stream = label is not None and label != current_label
            if label is not None:
                current_label = label
            if stage is not None:
                # A new stage line starts a fresh run of streams (next clip,
                # a retry): its first bar must get its own line instead of
                # overwriting the stage line just because the label repeats.
                current_label = None
            done = percent is not None and round(percent) >= 100
            push = (stage is not None or new_stream) and not adhd

            now = time.monotonic()
            if (not push and not done
                    and (now - last_edit_time) < MIN_EDIT_INTERVAL_SEC
                    and abs((percent or 0) - last_percent) < MIN_PERCENT_DELTA):
                return
            last_edit_time = now
            last_percent = percent or 0

            if percent is None:
                if adhd:
                    # No tool names, no speed/ETA - just a friendly holding
                    # line while progress is unknown (merging, converting...).
                    line = "One moment…"
                else:
                    meta = []
                    if speed:
                        meta.append(speed)
                    if eta and eta not in ("~", ""):
                        meta.append(f"ETA {eta}")
                    if stage and meta:
                        line = f"{stage} · {' · '.join(meta)}"
                    elif meta:
                        line = " · ".join(meta)
                    elif stage:
                        line = stage
                    else:
                        line = "Working..."
            elif stage and done:
                # Postprocessing stages ("Merging video & audio") report
                # 100% - the stage name is the message, not a bar.
                line = "Finishing up…" if adhd else stage
            else:
                # "Video - 42% • 🍬🍬🍬🍬◾️◾️◾️◾️◾️◾️ • 2.1MB/s"; at 100% just "Video - 100%".
                # ADHD Mode: no title and no speed.
                line = render_progress(bar_style, percent, None if adhd else speed,
                                       None if adhd else label)
                if stage and not adhd:
                    line = f"{stage} · {line}"

            asyncio.create_task(self._emit_progress(job, line, push, queued_menu(job.rid, job.url)))

        with job_workspace() as workspace:
            if job.settings.get("tool"):
                files = await media_tools.run(
                    job.settings, workspace,
                    lambda *args: progress_cb("tool", *args),         # (percent, speed, eta, stage, label)
                    job.cancel_event)
            else:
                files = await dispatch_download(job.url, workspace, job.settings, job.user_id, progress_cb,
                                                 cancel_event=job.cancel_event)

            # Name for /history: the first real file's name (a gallery/playlist
            # just gets its first item). Keep the preview title if there's none.
            named = next((f for f in files if f.suffix.lower() not in _NON_MEDIA_EXTS), None) \
                or (files[0] if files else None)
            if named is not None and named.stem:
                job.title = named.stem
                if job.settings.get("sections"):
                    # "Title [01-30-00–01-32-00]" -> "Title": the range isn't a title
                    job.title = re.sub(r"\s*\[[^\]]*\]$", "", job.title) or job.title

            if job.settings.get("mode") in ("video", "audio") and job.batch is None and not job.settings.get("tool"):
                self._drop_cached(job.rid)
                for f in files:
                    if f.suffix.lower() not in _NON_MEDIA_EXTS:
                        await self._cache_video_file(job.rid, job.url, f)

            job.header = "Sending"
            await self._emit(job, "Uploading to Telegram...", markup=queued_menu(job.rid, job.url))
            await self._send_files(job, files)
            if job.settings.get("tool"):
                media_tools.store.touch(job.rid)          # kept for another hour: more tools can be run on it
            # Things the downloader wants the person to know (e.g. subtitles couldn't be fetched).
            for note in job.settings.pop("delivery_notes", []):
                try:
                    await self.bot.send_message(job.chat_id, note)
                except TelegramError as exc:
                    log.warning("Could not send a delivery note for job %s: %s", job.rid, exc)

    async def _send_files(self, job: Job, files: list[Path]) -> None:
        # With several videos/audios (multi-clip job, playlist) the "send as
        # file" button goes on the LAST one only, labelled "all": it re-sends
        # the whole batch, so repeating it under every clip would be misleading.
        # Subtitle files go out AFTER the video, as plain documents, and don't count toward
        # "several files" (which decides where the send-as-file button goes).
        subtitle_files = [f for f in files if f.suffix.lower() in _SUBTITLE_EXTS]
        files = [f for f in files if f.suffix.lower() not in _SUBTITLE_EXTS]
        multi = len(files) > 1
        tool_job = bool(job.settings.get("tool"))
        # A toolbox result has no source link to copy and no cached original to "send as file" from.
        link_menu = None if tool_job else sent_menu(job.url)
        for index, f in enumerate(files):
            is_last = index == len(files) - 1
            suffix = f.suffix.lower()
            size_mb = f.stat().st_size / 1_000_000
            log.info("Sending rid=%s file=%s suffix=%s force_document=%s",
                      job.rid, f.name, suffix, job.force_document)

            if job.force_document:
                caption = messages.all_done_caption(f.stem[:100]) + "\n\nSent as a file — not re-compressed by Telegram."
                await self._emit(job, f"Sending {size_mb:.1f} MB...", markup=None)
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_document(
                        job.chat_id, input_file, caption=caption, parse_mode=ParseMode.HTML,
                        reply_markup=link_menu if (is_last or not multi) else None,
                        read_timeout=180, write_timeout=180, connect_timeout=60,
                    )
                continue

            caption = messages.all_done_caption(f.stem[:100])
            if multi:
                send_menu = send_as_file_menu(job.rid, job.url, "▤ Send all as files") if is_last else None
                video_note = "\n\nWant the original files instead of these compressed previews?"
                audio_note = "\n\nWant them sent as plain files instead?"
            else:
                send_menu = send_as_file_menu(job.rid, job.url)
                video_note = "\n\nWant the original file instead of this compressed preview?"
                audio_note = "\n\nWant it sent as a plain file instead?"
            if multi and not is_last:
                video_note = audio_note = ""
            if tool_job:
                send_menu, video_note, audio_note = None, "", ""
            if job.batch is not None:
                # "Send as file instead" re-runs ONE job from its own message and cache -
                # neither exists for a batch item, so don't offer a dead-end button.
                send_menu, video_note, audio_note = sent_menu(job.url), "", ""

            if tool_job and suffix == ".gif":
                with open(f, "rb") as fh:
                    await self.bot.send_animation(job.chat_id, InputFile(fh, filename=f.name), caption=caption,
                                                   parse_mode=ParseMode.HTML,
                                                   read_timeout=120, write_timeout=120, connect_timeout=60)
            elif suffix in _STREAMABLE_VIDEO_EXTS:
                await self._emit(job, f"Sending {size_mb:.1f} MB...", markup=None)
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_video(
                        job.chat_id, input_file, caption=caption + video_note, parse_mode=ParseMode.HTML,
                        supports_streaming=True, reply_markup=send_menu,
                        read_timeout=120, write_timeout=120, connect_timeout=60,
                    )
            elif suffix in {".mp3", ".m4a", ".opus", ".flac", ".wav"}:
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_audio(job.chat_id, input_file, caption=caption + audio_note,
                                               parse_mode=ParseMode.HTML, reply_markup=send_menu,
                                               read_timeout=120, write_timeout=120, connect_timeout=60)
            elif suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_photo(job.chat_id, input_file, caption=caption,
                                               parse_mode=ParseMode.HTML, reply_markup=link_menu)
            else:
                with open(f, "rb") as fh:
                    input_file = InputFile(fh, filename=f.name)
                    await self.bot.send_document(job.chat_id, input_file, caption=caption,
                                                  parse_mode=ParseMode.HTML, reply_markup=link_menu,
                                                  read_timeout=120, write_timeout=120, connect_timeout=60)
        for f in subtitle_files:
            parts = f.suffixes
            language = parts[-2].lstrip(".") if len(parts) >= 2 and _LANG_SUFFIX.match(parts[-2].lstrip(".")) else ""
            with open(f, "rb") as fh:
                await self.bot.send_document(job.chat_id, InputFile(fh, filename=f.name),
                                             caption=f"◧ Subtitles{f' · {language}' if language else ''}",
                                             read_timeout=120, write_timeout=120, connect_timeout=60)
        if job.batch is None:                    # the shared message belongs to the batch
            if tool_job and await self._restore_toolbox(job):
                return
            try:
                await self.bot.delete_message(job.chat_id, job.status_message_id)
            except TelegramError:
                pass

    async def _restore_toolbox(self, job: Job) -> bool:
        """After a toolbox result, turn the progress message back into the toolbox (the uploaded file is still on the
        server for an hour), so the next tool is one tap away. False = the file is gone: the caller cleans up."""
        item = media_tools.store.get(job.rid, job.user_id)
        if item is None:
            return False
        text, markup = tools_menu.toolbox_screen(item, job.rid)
        try:
            await self._safe_edit(job, text, markup)
        except TelegramError:
            return False
        return True

    async def _safe_edit(self, job: Job, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Edit the job's status message - strictly in order, newest wins.

        Every update used to be sent as its own concurrent request. On a slow
        link they finish out of order, so an older "42%" could land after a
        newer "80%" (the bar jumping backwards) or after the final "Didn't
        work" (replacing it with a stale progress line). Now there is at most
        one request in flight per job: new text replaces whatever is still
        waiting, and the sender loops until nothing newer is left, so the
        LAST thing rendered is always the last thing shown."""
        job.pending_edit = (text, markup)
        if job.editing:
            return                      # the running sender will pick this up
        job.editing = True
        try:
            while job.pending_edit is not None:
                text, markup = job.pending_edit
                job.pending_edit = None
                await self._send_edit(job, text, markup)
        finally:
            job.editing = False

    async def _send_edit(self, job: Job, text: str, markup: InlineKeyboardMarkup | None) -> None:
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
