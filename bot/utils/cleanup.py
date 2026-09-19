"""
Every download job gets its own throwaway folder under TMP_DIR.
We guarantee it's wiped afterwards (success, failure, or crash) so the VPS
disk never fills up with leftover media, .part files, or thumbnails.

CACHE_DIR is the one exception: "send as file" keeps a short-lived copy
of the just-downloaded file there so a re-request doesn't need a fresh
download - see jobqueue/job_manager.py for the actual expiry logic.
"""
import logging
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import TMP_DIR, CACHE_DIR

log = logging.getLogger("candy.cleanup")


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
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

