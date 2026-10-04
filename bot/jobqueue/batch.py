"""
Downloading several items (a playlist, or several pasted links) as ONE unit.

Every item still runs as an ordinary Job in the normal queue - same workers,
same cancel, same history - but they all report into a single shared status
message instead of one message each (twelve evolving messages would be chaos).

A Batch is also, deliberately, shaped like a Job as far as
JobManager._safe_edit is concerned (chat_id, status_message_id, is_photo,
pending_edit, editing, rid): that lets the shared message use the very same
ordered, newest-wins edit pump, so progress updates from several jobs can't
arrive out of order either.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from telegram.error import TelegramError

from config import OWNER_EMOJI
from ui import batch_menu
from ui.steplog import symbol_for
from utils.text import esc, site_label

log = logging.getLogger("candy.batch")

BATCH_MIN_EDIT_INTERVAL = 2.0     # seconds between edits of the shared message (Telegram rate limits)
_FINAL_STATES = ("done", "failed", "cancelled")
_TITLE_CHARS = 26
_MAX_FAILURES_LISTED = 5


@dataclass
class BatchItem:
    url: str
    title: str = ""
    duration: int | None = None
    state: str = "waiting"        # waiting | running | done | failed | cancelled
    percent: float | None = None
    line: str = ""                # the latest status text while it runs
    error: str = ""
    job_rid: str = ""


@dataclass
class Batch:
    bid: str
    user_id: int
    chat_id: int
    status_message_id: int
    items: list[BatchItem]
    title: str = ""
    kind: str = "links"           # "playlist" | "links"
    is_photo: bool = False
    settings: dict = field(default_factory=dict)       # what the batch was started with (mode, quality, ...)
    selected: set[int] = field(default_factory=set)
    page: int = 0
    run_indices: list[int] = field(default_factory=list)
    finished: bool = False
    closed: bool = False          # the shared message is gone (deleted / dismissed)
    cancel_requested: bool = False
    # --- shaped like a Job for JobManager._safe_edit ---
    pending_edit: tuple | None = field(default=None, repr=False)
    editing: bool = field(default=False, repr=False)
    # --- internals ---
    _index: dict = field(default_factory=dict, repr=False)      # job rid -> item index
    _last_done: int | None = field(default=None, repr=False)
    _last_edit: float = field(default=-1e9, repr=False)
    _flush: asyncio.Task | None = field(default=None, repr=False)
    _manager: object = field(default=None, repr=False)

    @property
    def rid(self) -> str:
        return self.bid

    # ------------------------------------------------------------ running
    def _run_items(self) -> list[BatchItem]:
        return [self.items[i] for i in self.run_indices]

    async def start(self, manager, settings_for, indices: list[int]) -> None:
        """Queue the chosen items. `settings_for(url)` gives each one its own
        settings (e.g. a Spotify link must be audio whatever was picked)."""
        self._manager = manager
        self.run_indices = list(indices)
        self.finished = False
        await self._enqueue(manager, settings_for, self.run_indices)
        await self.refresh(manager, force=True)

    async def retry_failed(self, manager, settings_for) -> int:
        failed = [i for i in self.run_indices if self.items[i].state == "failed"]
        for i in failed:
            self.items[i].error = ""
        self.finished = False
        self.cancel_requested = False
        await self._enqueue(manager, settings_for, failed)
        await self.refresh(manager, force=True)
        return len(failed)

    async def _enqueue(self, manager, settings_for, indices: list[int]) -> None:
        for index in indices:
            item = self.items[index]
            item.state, item.percent, item.line = "waiting", None, ""
            item.job_rid = uuid.uuid4().hex[:10]
            self._index[item.job_rid] = index
            await manager.enqueue(item.job_rid, self.user_id, self.chat_id, item.url, dict(settings_for(item.url)),
                                  self.status_message_id, title=item.title, batch=self)

    def cancel(self, manager) -> int:
        """Stop everything still waiting or running. Items already delivered stay delivered."""
        self.cancel_requested = True
        count = 0
        for item in self._run_items():
            if item.state in ("waiting", "running") and manager.cancel(item.job_rid, self.user_id):
                count += 1
        return count

    # ------------------------------------------------------------ updates from jobs
    def note(self, job) -> None:
        """A job's status changed - copy what the shared message needs."""
        index = self._index.get(job.rid)
        if index is None:
            return
        item = self.items[index]
        if item.state == "waiting" and job.task is not None:
            item.state = "running"
        item.percent = job.percent
        item.line = job.steps[-1] if job.steps else ""

    async def job_finished(self, manager, job) -> None:
        index = self._index.get(job.rid)
        if index is None:
            return
        item = self.items[index]
        item.state = {"success": "done", "failed": "failed"}.get(job.outcome, "cancelled")
        item.percent, item.line = None, ""
        item.error = job.error_text if item.state == "failed" else ""
        if item.state == "done":
            self._last_done = index
        if all(it.state in _FINAL_STATES for it in self._run_items()):
            await self._complete(manager)
        else:
            await self.refresh(manager)

    async def _complete(self, manager) -> None:
        self.finished = True
        clean = all(it.state == "done" for it in self._run_items())
        if clean:
            await self.close(manager)         # everything arrived: no leftover message, like a single download
        else:
            await self.refresh(manager, force=True)

    async def close(self, manager) -> None:
        """Delete the shared message once any in-flight edit has landed."""
        self.closed = True
        for _ in range(100):
            if not self.editing:
                break
            await asyncio.sleep(0.05)
        try:
            await manager.bot.delete_message(self.chat_id, self.status_message_id)
        except TelegramError:
            pass

    # ------------------------------------------------------------ the shared message
    async def refresh(self, manager, force: bool = False) -> None:
        if self.closed:
            return
        self._manager = manager
        wait = BATCH_MIN_EDIT_INTERVAL - (time.monotonic() - self._last_edit)
        if wait > 0 and not force:
            if self._flush is None or self._flush.done():
                self._flush = asyncio.create_task(self._flush_after(wait))     # one trailing update, so the last state always lands
            return
        await self._send()

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self.closed:
            await self._send()

    async def _send(self) -> None:
        self._last_edit = time.monotonic()
        await self._manager._safe_edit(self, self.render(), self.markup())

    def markup(self):
        if not self.finished:
            return batch_menu.running_menu(self)
        failed = sum(1 for it in self._run_items() if it.state == "failed")
        return batch_menu.summary_menu(self, failed)

    def render(self) -> str:
        run = self._run_items()
        total = len(run)
        done = sum(1 for it in run if it.state == "done")
        failed = [it for it in run if it.state == "failed"]
        cancelled = sum(1 for it in run if it.state == "cancelled")

        if not self.finished:
            header = f"Downloading · {done} of {total} done"
        elif self.cancel_requested:
            header = f"Cancelled · {done} of {total} delivered"
        else:
            header = f"Finished · {done} of {total} delivered"

        def name(index: int) -> str:
            title = " ".join((self.items[index].title or "").split())
            return f"{index + 1}. {title[:_TITLE_CHARS] + '…' if len(title) > _TITLE_CHARS else title}"

        lines = []
        if not self.finished:
            if self._last_done is not None and self.items[self._last_done].state == "done":
                lines.append(f"[✓] {name(self._last_done)}")
            for index in self.run_indices:
                item = self.items[index]
                if item.state == "running":
                    symbol = symbol_for(item.line) if item.line else "⌲"
                    pct = f" · {round(item.percent)}%" if item.percent is not None else ""
                    lines.append(f"[{symbol}] {name(index)}{pct}")
            waiting = sum(1 for it in run if it.state == "waiting")
            if waiting:
                lines.append(f"[ⴵ] {waiting} waiting")
            if failed:
                lines.append(f"[✕] {len(failed)} failed")
        else:
            lines.append(f"[✓] {done} delivered")
            if failed:
                lines.append(f"[✕] {len(failed)} failed")
            if cancelled:
                lines.append(f"[✕] {cancelled} cancelled")

        parts = [f"{OWNER_EMOJI} <b>{esc(header)}</b>"]
        if lines:
            parts.append("<pre>" + esc("\n".join(lines)) + "</pre>")
        if self.finished and failed:
            detail = [f"{esc(name(self.items.index(it)))} — {esc(it.error or 'failed')}" for it in failed[:_MAX_FAILURES_LISTED]]
            if len(failed) > _MAX_FAILURES_LISTED:
                detail.append(f"+{len(failed) - _MAX_FAILURES_LISTED} more")
            parts.append("✕ Didn't download:\n" + "\n".join(detail))
        parts.append(self._info_line(total))
        return "\n\n".join(parts)

    def _info_line(self, total: int) -> str:
        audio = self.settings.get("mode") == "audio"
        site = site_label(self.items[0].url) if self.items else ""
        pieces = ["🎵 Audio" if audio else "🎬 Video", f"📋 {total} item{'s' if total != 1 else ''}"]
        if site:
            pieces.append(f"🔗 {site}")
        return "↳ " + " | ".join(pieces)
