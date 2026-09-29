"""
Test double for yt_dlp (the real one needs network + a real video). Only what
the clip code touches. Behaviour is scripted per call through SCRIPT:

    "ok"        write a real, tiny clip with ffmpeg, of the requested length
    "fail"      raise a generic error
    "botcheck"  raise YouTube's "confirm you're not a bot" error
    "hang"      behave like a stuck ffmpeg child: start a process NAMED ffmpeg
                that mentions the output folder, wait for it, and fail the way
                yt-dlp does when that child gets killed
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import utils  # noqa: F401  (yt_dlp.utils.X is used by the code under test)

REAL_FFMPEG = shutil.which("ffmpeg")
SCRIPT: list[str] = []
CALLS: list[dict] = []
TITLE = "A long title cut mid-sentence "     # trailing space, like %(title).60B can leave


def reset(script: list[str]) -> None:
    SCRIPT[:] = script
    CALLS.clear()


class YoutubeDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        index = len(CALLS)
        CALLS.append(self.opts)
        action = SCRIPT[index] if index < len(SCRIPT) else "ok"
        target = Path(self.opts["outtmpl"].replace("%(title).60B", TITLE).replace("%(ext)s", "mp4"))
        target.parent.mkdir(parents=True, exist_ok=True)

        if action == "fail":
            raise Exception("boom")
        if action == "botcheck":
            raise Exception("Sign in to confirm you’re not a bot")
        if action == "hang":
            fake = Path(tempfile.mkdtemp()) / "ffmpeg"
            fake.symlink_to(sys.executable)
            child = subprocess.Popen([str(fake), "-c", "import time; time.sleep(60)", str(target.parent / "x.mp4")])
            code = child.wait()
            raise Exception(f"ffmpeg exited with code {code}")

        section = self.opts["download_ranges"]({}, self)[0]
        length = section["end_time"] - section["start_time"]
        subprocess.run(
            [REAL_FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=d={length}:s=64x48:r=5",
             "-f", "lavfi", "-i", f"sine=d={length}", "-shortest", "-pix_fmt", "yuv420p", str(target)],
            check=True,
        )
        for hook in self.opts.get("progress_hooks", []):
            hook({"status": "finished", "info_dict": {}})
