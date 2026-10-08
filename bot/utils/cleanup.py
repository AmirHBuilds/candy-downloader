"""
Every download job gets its own throwaway folder under TMP_DIR.
We guarantee it's wiped afterwards (success, failure, or crash) so the VPS
disk never fills up with leftover media, .part files, or thumbnails.

CACHE_DIR is the one exception: "send as file" keeps a short-lived copy
of the just-downloaded file there so a re-request doesn't need a fresh
download - see jobqueue/job_manager.py for the actual expiry logic.
"""
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import TMP_DIR, CACHE_DIR
from utils.procs import kill_processes_under

log = logging.getLogger("candy.cleanup")

# Folder names of the jobs running right now: housekeeping must not treat their processes as orphans.
ACTIVE_WORKSPACES: set[str] = set()

# The only places inside the local Bot API server's storage the bot ever touches: the files people sent.
# (Its own database / binlog files live next to these and must never be deleted.)
SERVER_FILE_DIRS = ("documents", "photos", "videos", "video_notes", "voice", "audios", "animations",
                    "stickers", "thumbnails", "temp")


def sweep_orphaned_workspaces() -> None:
    """Run once at startup: wipe any job folders (and any leftover cached
    files - their in-memory expiry tracking is gone anyway after a
    restart) left behind by an unclean shutdown."""
    root = Path(TMP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            log.info("Swept orphaned workspace: %s", child)
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)


def new_cache_path(suffix: str) -> Path:
    """A fresh, unique path under CACHE_DIR for stashing one file."""
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(CACHE_DIR) / f"{uuid.uuid4().hex}{suffix}"


@contextmanager
def job_workspace():
    """Context manager yielding a fresh empty directory. Deleted on exit
    no matter what happens inside the `with` block."""
    workspace = Path(TMP_DIR) / uuid.uuid4().hex
    workspace.mkdir(parents=True, exist_ok=True)
    ACTIVE_WORKSPACES.add(workspace.name)
    try:
        yield workspace
    finally:
        ACTIVE_WORKSPACES.discard(workspace.name)
        # Whatever the job was running (ffmpeg merging/converting, aria2c) must not outlive it:
        # an orphan can sit at 0% CPU holding hundreds of MB.
        stray = kill_processes_under(workspace)
        if stray:
            log.info("Reaped %d stray process(es) left over from %s", stray, workspace.name)
        shutil.rmtree(workspace, ignore_errors=True)



def _server_file_dirs(root: Path):
    """Every <root>/<dir> and <root>/<bot-token>/<dir> that holds sent files."""
    bases = [root] + [child for child in root.iterdir() if child.is_dir() and child.name not in SERVER_FILE_DIRS]
    for base in bases:
        for name in SERVER_FILE_DIRS:
            folder = base / name
            if folder.is_dir():
                yield folder


def sweep_bot_api_files(root: str | Path, max_age_seconds: float, now: float | None = None) -> tuple[int, int]:
    """Delete files older than max_age_seconds from the local Bot API server's storage (only the folders in
    SERVER_FILE_DIRS, only regular files). Returns (files deleted, bytes freed)."""
    root = Path(root)
    if not root.is_dir():
        return 0, 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    count = freed = 0
    for folder in _server_file_dirs(root):
        for path in folder.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    path.unlink()
                    count += 1
                    freed += stat.st_size
            except OSError:
                continue
    return count, freed


def drop_server_copy(file_path: str | None, root: str | Path | None = None) -> bool:
    """Delete the local Bot API server's copy of a file the bot has just made its own copy of. Only ever deletes
    a regular file inside one of SERVER_FILE_DIRS under the server's storage. True = deleted."""
    if not file_path:
        return False
    try:
        from config import BOT_API_DATA_DIR
        base = Path(root or BOT_API_DATA_DIR).resolve()
        path = Path(file_path).resolve()
        if base not in path.parents or path.parent.name not in SERVER_FILE_DIRS or not path.is_file():
            return False
        path.unlink()
        return True
    except OSError:
        return False
