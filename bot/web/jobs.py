"""
The web app's view of jobs: who owns which job, what it looked like when it finished, and the files it left.
The work itself is done by the shared JobManager; this keeps only what the browser needs.
"""
import asyncio
import html
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from utils.webchat import chat_id_for, deliveries

_TAGS = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """Job messages are Telegram HTML (and emoji bars); the web shows them as text."""
    return html.unescape(_TAGS.sub("", text or "")).strip()


@dataclass
class Rec:
    rid: str
    account_id: int
    user_id: int
    url: str
    title: str
    kind: str                       # "download" | "tool"
    settings: dict = field(repr=False)
    created: float = field(default_factory=time.time)
    outcome: str = ""               # "" while active, then success | failed | cancelled
    error: str = ""
    finished: float = 0.0
    last_line: str = ""
    steps: list = field(default_factory=list)


class SubmitError(Exception):
    def __init__(self, message: str, status: int = 429) -> None:
        super().__init__(message)
        self.status = status


class WebJobs:
    def __init__(self, manager, ttl_seconds: float, quota_bytes: int, max_active: int, root: Path) -> None:
        self.manager, self.ttl, self.quota, self.max_active, self.root = manager, ttl_seconds, quota_bytes, max_active, root
        self.records: dict[str, Rec] = {}
        manager.finish_listeners.append(self._on_finish)

    # ------------------------------------------------------------ submitting
    def active_for(self, account_id: int) -> int:
        return sum(1 for r in self.records.values() if r.account_id == account_id and not r.outcome)

    def disk_used(self, account_id: int) -> int:
        return sum(f["size"] for r in self.records.values() if r.account_id == account_id
                   for f in deliveries.files.get(r.rid, []))

    async def submit(self, account, url: str, settings: dict, title: str = "", kind: str = "download",
                     rid: str | None = None) -> Rec:
        if self.active_for(account.id) >= self.max_active:
            raise SubmitError(f"You already have {self.max_active} things running. Wait for one to finish.")
        if self.disk_used(account.id) >= self.quota:
            raise SubmitError("Your finished files take up your whole allowance. Download or remove some first.")
        rid = rid or secrets.token_hex(8)
        rec = Rec(rid, account.id, account.user_id, url, title, kind, settings)
        self.records[rid] = rec
        await self.manager.enqueue(rid, account.user_id, chat_id_for(account.id), url, dict(settings), 0, title=title)
        return rec

    async def retry(self, rec: Rec, account) -> Rec:
        if not rec.outcome or rec.outcome == "success":
            raise SubmitError("Only a failed or cancelled job can be tried again.", 409)
        if self.active_for(account.id) >= self.max_active:
            raise SubmitError(f"You already have {self.max_active} things running. Wait for one to finish.")
        rec.outcome, rec.error, rec.finished, rec.steps, rec.last_line = "", "", 0.0, [], ""
        await self.manager.enqueue(rec.rid, rec.user_id, chat_id_for(rec.account_id), rec.url, dict(rec.settings), 0,
                                   title=rec.title)
        return rec

    # ------------------------------------------------------------ reading
    def get(self, rid: str, account_id: int) -> Rec | None:
        rec = self.records.get(rid)
        return rec if rec and rec.account_id == account_id else None

    def for_account(self, account_id: int) -> list[Rec]:
        return sorted((r for r in self.records.values() if r.account_id == account_id),
                      key=lambda r: r.created, reverse=True)

    def view(self, rec: Rec) -> dict:
        live = self.manager.job_snapshot(rec.rid) if not rec.outcome else None
        if live is not None:
            running = bool(live.task and not live.task.done())
            state = "running" if running else "queued"
            steps = [plain(s) for s in live.steps[-6:]]
            percent = live.percent
            info = {k: v for k, v in live.info.items() if k in ("type", "size", "site")}
            title = live.title or rec.title
        else:
            state = {"": "running", "success": "done"}.get(rec.outcome, rec.outcome)
            steps, percent, info, title = rec.steps, 100 if rec.outcome == "success" else None, {}, rec.title
        files = [{"index": i, "name": f["name"], "size": f["size"]}
                 for i, f in enumerate(deliveries.files.get(rec.rid, []))]
        return {"rid": rec.rid, "url": rec.url if rec.kind == "download" else "", "title": title, "kind": rec.kind,
                "state": state, "percent": percent, "steps": steps, "info": info, "error": rec.error,
                "files": files, "notes": list(deliveries.notes.get(rec.rid, [])), "created": rec.created,
                "expires": (rec.finished + self.ttl) if rec.finished else None}

    def file_for(self, rid: str, account_id: int, index: int) -> dict | None:
        rec = self.get(rid, account_id)
        files = deliveries.files.get(rid, []) if rec else []
        if rec is None or not 0 <= index < len(files):
            return None
        entry = files[index]
        return entry if entry["path"].is_file() else None

    # ------------------------------------------------------------ finishing and cleaning up
    def _on_finish(self, job) -> None:
        rec = self.records.get(job.rid)
        if rec is None:
            return
        rec.outcome = job.outcome or "failed"
        rec.error = job.error_text or ""
        rec.title = job.title or rec.title
        rec.finished = time.time()
        rec.steps = [plain(s) for s in job.steps[-6:]]
        rec.last_line = rec.steps[-1] if rec.steps else ""

    def cancel(self, rec: Rec) -> bool:
        return self.manager.cancel(rec.rid, rec.user_id)

    def discard(self, rid: str) -> None:
        """Forget a job and delete its files."""
        for path in deliveries.drop(rid):
            path.unlink(missing_ok=True)
        shutil.rmtree(self.root / rid, ignore_errors=True)
        self.records.pop(rid, None)

    def discard_account(self, account_id: int) -> None:
        for rec in [r for r in self.records.values() if r.account_id == account_id]:
            self.manager.cancel(rec.rid, rec.user_id)
            self.discard(rec.rid)

    def sweep(self, now: float | None = None) -> int:
        now = now or time.time()
        old = [r.rid for r in self.records.values() if r.outcome and now - r.finished > self.ttl]
        for rid in old:
            self.discard(rid)
        return len(old)

    async def sweep_loop(self, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                self.sweep()
            except Exception:  # noqa: BLE001
                pass
