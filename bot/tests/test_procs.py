"""No ffmpeg may outlive its job: reaping by workspace, on cancel, on timeout and at teardown."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
from downloader import ytdlp_handler  # noqa: E402
from downloader.errors import JobCancelled  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from utils import cleanup  # noqa: E402
from utils.procs import kill_processes_under  # noqa: E402


class Fakes:
    """Processes that look like ffmpeg (or something else) to /proc, and just sleep."""

    def __init__(self, test: unittest.TestCase):
        self.dir = Path(tempfile.mkdtemp(prefix="procs-bin-"))
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.procs: list[subprocess.Popen] = []
        test.addCleanup(self.cleanup)

    def spawn(self, name: str, *args: str) -> subprocess.Popen:
        exe = self.dir / name
        if not exe.exists():
            exe.symlink_to(sys.executable)
        proc = subprocess.Popen([str(exe), "-c", "import time; time.sleep(60)", *args],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        time.sleep(0.15)                       # let it exec so /proc shows its real command line
        return proc

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


class Reaping(unittest.TestCase):
    def setUp(self):
        self.fakes = Fakes(self)
        self.workspace = Path(tempfile.mkdtemp(prefix="procs-ws-"))
        self.other = Path(tempfile.mkdtemp(prefix="procs-other-"))
        for d in (self.workspace, self.other):
            self.addCleanup(shutil.rmtree, d, ignore_errors=True)

    def test_kills_the_jobs_ffmpeg_and_aria2c_but_nothing_else(self):
        mine = self.fakes.spawn("ffmpeg", str(self.workspace / "in.mp4"))
        aria = self.fakes.spawn("aria2c", f"--dir={self.workspace}/dl")
        theirs = self.fakes.spawn("ffmpeg", str(self.other / "in.mp4"))           # another job's
        unrelated = self.fakes.spawn("python3", str(self.workspace / "x"))        # not a media tool
        self.assertEqual(kill_processes_under(self.workspace), 2)
        mine.wait(timeout=3)
        aria.wait(timeout=3)
        self.assertIsNone(theirs.poll())
        self.assertIsNone(unrelated.poll())

    def test_a_workspace_that_is_only_a_prefix_of_another_is_not_confused_with_it(self):
        sibling = Path(str(self.workspace) + "2")
        sibling.mkdir()
        self.addCleanup(shutil.rmtree, sibling, ignore_errors=True)
        neighbour = self.fakes.spawn("ffmpeg", str(sibling / "in.mp4"))
        self.assertEqual(kill_processes_under(self.workspace), 0)
        self.assertIsNone(neighbour.poll())

    def test_nothing_running_is_fine(self):
        self.assertEqual(kill_processes_under(self.workspace), 0)

    def test_leaving_a_job_workspace_reaps_what_was_left_running(self):
        with cleanup.job_workspace() as workspace:
            stray = self.fakes.spawn("ffmpeg", str(workspace / "merge.mp4"))
            self.assertIsNone(stray.poll())
        stray.wait(timeout=3)                                    # killed on the way out
        self.assertFalse(workspace.exists())                     # and the folder is gone as before

    def test_even_a_crash_inside_the_job_reaps(self):
        stray = None
        with self.assertRaises(RuntimeError):
            with cleanup.job_workspace() as workspace:
                stray = self.fakes.spawn("ffmpeg", str(workspace / "x.mp4"))
                raise RuntimeError("job blew up")
        stray.wait(timeout=3)


class CancelledDownload(unittest.IsolatedAsyncioTestCase):
    """A normal (not clip) download whose ffmpeg is stuck: Cancel must stop it promptly and report a cancel."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="procs-dl-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        yt_dlp.reset(["hang"])

    async def test_cancel_stops_the_stuck_ffmpeg_and_reports_cancelled(self):
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(1.5, cancel.set)
        started = time.monotonic()
        with self.assertRaises(JobCancelled):
            await ytdlp_handler.download("https://youtu.be/x", self.workspace,
                                         dict(DEFAULTS, mode="video", quality="best"), 1, lambda *a: None,
                                         cancel_event=cancel)
        self.assertLess(time.monotonic() - started, 8)                     # not the fake's 60 s
        left = subprocess.run(["pgrep", "-f", str(self.workspace) + "/"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")

    async def test_a_timeout_also_kills_the_ffmpeg(self):
        self.addCleanup(setattr, ytdlp_handler, "DOWNLOAD_TIMEOUT_SECONDS", ytdlp_handler.DOWNLOAD_TIMEOUT_SECONDS)
        ytdlp_handler.DOWNLOAD_TIMEOUT_SECONDS = 1
        with self.assertRaisesRegex(RuntimeError, "Timed out"):
            await ytdlp_handler.download("https://youtu.be/x", self.workspace,
                                         dict(DEFAULTS, mode="video", quality="best"), 1, lambda *a: None)
        await asyncio.sleep(0.3)
        left = subprocess.run(["pgrep", "-f", str(self.workspace) + "/"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")


if __name__ == "__main__":
    unittest.main()
