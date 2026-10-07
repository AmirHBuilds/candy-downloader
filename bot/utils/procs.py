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
import os
import signal
from pathlib import Path

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
