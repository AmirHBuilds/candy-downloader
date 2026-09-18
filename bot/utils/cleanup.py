"""
Every download job gets its own throwaway folder under TMP_DIR.
We guarantee it's wiped afterwards (success, failure, or crash) so the VPS
disk never fills up with leftover media, .part files, or thumbnails.
"""
import logging
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import TMP_DIR

log = logging.getLogger("candy.cleanup")


def sweep_orphaned_workspaces() -> None:
    """Run once at startup: wipe any job folders left behind by an unclean
    shutdown (e.g. the container was killed mid-download)."""
    root = Path(TMP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            log.info("Swept orphaned workspace: %s", child)


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
