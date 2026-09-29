"""
Clip ("sections") download path, against a stub yt_dlp that writes real clips
with ffmpeg. Run from bot/:   python -m unittest discover -s tests
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "test-token")   # config.py refuses to import without one
os.environ.setdefault("OWNER_USER_ID", "1")

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "stubs"))   # stub BEFORE any real yt_dlp
sys.path.insert(0, str(BOT_DIR))

import yt_dlp  # noqa: E402  (the stub)
from downloader import ytdlp_handler, dispatcher  # noqa: E402
from downloader.errors import JobCancelled  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402


def seconds_of(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "json", str(path)], capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg + ffprobe")
class ClipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="cliptest-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.calls: list[tuple] = []
        self.settings = dict(DEFAULTS, mode="video", quality="best",
                             sections=[(0.0, 5.0), (10.0, 13.0)])

    def cb(self, *args):
        self.calls.append(args)

    def stages(self) -> list[str]:
        return [a[3] for a in self.calls if len(a) > 3 and a[3]]

    async def run_clips(self, **overrides):
        yt_dlp.reset(overrides.pop("script", []))
        self.settings.update(overrides.pop("settings", {}))
        return await ytdlp_handler.download("https://youtu.be/x", self.workspace, self.settings, 1, self.cb,
                                            cancel_event=overrides.pop("cancel_event", None))

    async def test_one_run_per_section_with_exact_cuts(self):
        files = await self.run_clips()
        self.assertEqual(len(files), 2)
        self.assertEqual(len(yt_dlp.CALLS), 2)                       # one yt-dlp run per section
        self.assertTrue(all(c["force_keyframes_at_cuts"] for c in yt_dlp.CALLS))
        self.assertEqual(len({c["outtmpl"] for c in yt_dlp.CALLS}), 2)   # never the same output path
        self.assertAlmostEqual(seconds_of(files[0]), 5, delta=0.6)
        self.assertAlmostEqual(seconds_of(files[1]), 3, delta=0.6)
        self.assertEqual(files[0].name, "A long title cut mid-sentence [00-00-00–00-00-05].mp4")  # no double space
        self.assertEqual({p.name for p in self.workspace.iterdir()}, {f.name for f in files})    # clip dirs gone
        self.assertEqual(self.stages()[0], "Clip 1 of 2 · 0:00 – 0:05")
        self.assertIn("Clip 2 of 2 · 0:10 – 0:13", self.stages())

    async def test_single_section_wording(self):
        await self.run_clips(settings={"sections": [(2.0, 4.0)]})
        self.assertEqual(self.stages()[0], "Clip · 0:02 – 0:04")

    async def test_merge_joins_clips_in_order(self):
        files = await self.run_clips(settings={"sections_merge": True})
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "A long title cut mid-sentence [2 clips].mp4")
        self.assertAlmostEqual(seconds_of(files[0]), 8, delta=1.0)
        self.assertEqual(list(self.workspace.iterdir()), files)       # source clips + list file removed
        self.assertIn("Merging clips", self.stages())

    async def test_merge_ignored_for_a_single_section(self):
        files = await self.run_clips(settings={"sections": [(0.0, 4.0)], "sections_merge": True})
        self.assertEqual(len(files), 1)
        self.assertNotIn("Merging clips", self.stages())

    async def test_failed_clip_fails_the_job_and_names_it(self):
        with self.assertRaisesRegex(RuntimeError, "Clip 2 failed: boom"):
            await self.run_clips(script=["ok", "fail"])

    async def test_bot_check_retries_with_another_player_client(self):
        files = await self.run_clips(script=["botcheck", "ok", "ok"])
        self.assertEqual(len(files), 2)
        self.assertEqual(yt_dlp.CALLS[1]["extractor_args"]["youtube"]["player_client"], ["tv"])

    async def test_cancel_kills_ffmpeg_mid_clip(self):
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(1.5, cancel.set)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(JobCancelled):
            await self.run_clips(script=["hang"], cancel_event=cancel)
        self.assertLess(asyncio.get_running_loop().time() - started, 8)   # not the fake's 60s sleep
        left = subprocess.run(["pgrep", "-f", str(self.workspace)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")                                        # nothing left running

    async def test_cancel_before_start_does_nothing(self):
        cancel = asyncio.Event()
        cancel.set()
        with self.assertRaises(JobCancelled):
            await self.run_clips(cancel_event=cancel)
        self.assertEqual(yt_dlp.CALLS, [])

    async def test_bad_sections_fail_with_readable_message(self):
        with self.assertRaisesRegex(RuntimeError, "after the start"):
            await self.run_clips(settings={"sections": [(9.0, 3.0)]})


class DispatcherClipTests(unittest.IsolatedAsyncioTestCase):
    async def test_clip_request_never_falls_back_to_a_full_download(self):
        seen = []

        async def failing_ytdlp(*a, **k):
            seen.append("ytdlp")
            raise Exception("nope")

        async def must_not_run(*a, **k):
            seen.append("OTHER TOOL")
            return [Path("/tmp/whole-video.mp4")]

        original = dict(dispatcher.HANDLERS)
        dispatcher.HANDLERS.update(ytdlp=failing_ytdlp, gallerydl=must_not_run, generic=must_not_run)
        self.addCleanup(dispatcher.HANDLERS.update, original)
        with self.assertRaises(dispatcher.NoToolSucceeded):
            await dispatcher.download("https://example.com/some/video", Path("/tmp"),
                                      {"sections": [(0.0, 5.0)]}, 1, lambda *a, **k: None)
        self.assertEqual(seen, ["ytdlp"])

    async def test_clip_request_for_a_non_ytdlp_site_is_refused(self):
        async def must_not_run(*a, **k):
            raise AssertionError("should not run")

        original = dict(dispatcher.HANDLERS)
        dispatcher.HANDLERS.update({k: must_not_run for k in original})
        self.addCleanup(dispatcher.HANDLERS.update, original)
        with self.assertRaises(dispatcher.NoToolSucceeded) as ctx:
            await dispatcher.download("https://open.spotify.com/track/abc", Path("/tmp"),
                                      {"sections": [(0.0, 5.0)]}, 1, lambda *a, **k: None)
        self.assertIn("aren't supported", ctx.exception.primary_error)


if __name__ == "__main__":
    unittest.main()
