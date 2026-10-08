"""
Reaping stray child processes.

yt-dlp runs ffmpeg for merging, converting, embedding thumbnails and cutting
clips. When a job is cancelled, times out or fails, yt-dlp's worker thread can't
be interrupted from outside, so an ffmpeg it started may keep running - or sit
blocked, idle at 0% CPU but still holding hundreds of MB. Nothing waited for it.

Every such process has the job's own workspace folder in its command line (the
folder name is a fresh uuid per job), so "everything running against this
folder" identifies exactly one job's leftovers without touching anyone else's.
"""
import logging
import os
import re
import signal
from pathlib import Path

log = logging.getLogger("candy.procs")

REAPED = ("ffmpeg", "ffprobe", "aria2c")


def kill_processes_under(workspace: Path, names: tuple[str, ...] = REAPED) -> int:
    """SIGKILL every process whose executable is one of `names` and whose command
    line mentions this workspace. Returns how many were killed. Reads /proc, so it
    is a harmless no-op anywhere that doesn't have one."""
    needle = (str(workspace).rstrip("/") + "/").encode()
    me = os.getpid()
    killed = 0
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            pid = int(cmdline.parent.name)
            argv = cmdline.read_bytes().split(b"\0")
            if pid == me or not argv:
                continue
            executable = os.path.basename(argv[0]).decode("utf-8", "replace")
            if not any(name in executable for name in names):
                continue
            if needle not in b" ".join(argv):
                continue
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except (OSError, ValueError):
            continue            # it exited meanwhile, or it isn't ours to touch
    return killed


def _argv(pid_dir: Path) -> list[bytes]:
    return pid_dir.joinpath("cmdline").read_bytes().split(b"\0")


def _is_ours(argv: list[bytes]) -> bool:
    """ffmpeg / ffprobe / aria2c, or gallery-dl (a Python script, so its executable is python)."""
    if not argv:
        return False
    executable = os.path.basename(argv[0]).decode("utf-8", "replace")
    if any(name in executable for name in REAPED):
        return True
    return any(b"gallery-dl" in os.path.basename(arg) for arg in argv[:3])


def kill_orphans(tmp_dir: str | Path, active: set[str]) -> int:
    """The safety net behind per-job reaping: kill every downloader/ffmpeg process whose command line points
    into a job folder under tmp_dir that is NOT an active job any more (its job ended, crashed, or was
    cancelled and something - e.g. a yt-dlp worker thread - started another process afterwards).
    Job folders are 32-hex-digit names; everything else (the toolbox's uploads folder) is left alone."""
    pattern = re.compile(re.escape(str(tmp_dir).rstrip("/")).encode() + rb"/([0-9a-f]{32})(?:/|\b)")
    me = os.getpid()
    killed = 0
    for pid_dir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(pid_dir.name)
            if pid == me:
                continue
            argv = _argv(pid_dir)
            if not _is_ours(argv):
                continue
            match = pattern.search(b" ".join(argv))
            if match is None or match.group(1).decode() in active:
                continue
            os.kill(pid, signal.SIGKILL)
            killed += 1
            log.info("Killed orphaned process %d (%s)", pid, os.path.basename(argv[0]).decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
    return killed


def zombies() -> dict[int, int]:
    """pid -> parent pid, for every defunct (zombie) process: finished, but nobody has collected its exit status."""
    found = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat.read_text()
            fields = text[text.rindex(")") + 2:].split()       # after "pid (name) ": state, ppid, ...
            if fields[0] == "Z":
                found[int(stat.parent.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    return found


class ZombieReaper:
    """Collects zombie children of THIS process that nobody else collected.

    asyncio normally reaps its own children within a moment, so a zombie child that is still there at the next
    sweep is one nobody is waiting for. (Orphans re-parented to PID 1 are reaped by Docker's init - see
    `init: true` in docker-compose.yml - so a zombie we cannot collect is only reported.)"""

    def __init__(self) -> None:
        self._seen: dict[int, int] = {}

    def sweep(self) -> tuple[int, int]:
        """(zombies collected, zombies present)."""
        present = zombies()
        self._seen = {pid: count + 1 for pid, count in ((p, self._seen.get(p, 0)) for p in present)}
        me, reaped = os.getpid(), 0
        for pid, parent in present.items():
            if parent == me and self._seen[pid] >= 2:
                try:
                    os.waitpid(pid, os.WNOHANG)
                    reaped += 1
                except OSError:
                    pass                      # already collected in the meantime
        return reaped, len(present)
