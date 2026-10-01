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


class ProgressBarTests(ClipTests.__bases__[0]):
    """The bar for clip downloads: ffmpeg's own -progress output, with elapsed time as the fallback."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="cliptest-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.calls: list[tuple] = []
        self.settings = dict(DEFAULTS, mode="video", quality="best", sections=[(0.0, 4.0)])
        for name, value in (("_WATCH_INTERVAL", 0.05), ("_ELAPSED_EVERY_TICKS", 2)):
            self.addCleanup(setattr, ytdlp_handler, name, getattr(ytdlp_handler, name))
            setattr(ytdlp_handler, name, value)

    def cb(self, *args):
        self.calls.append(args)

    async def run_clip(self, script, progress):
        yt_dlp.reset(script)
        yt_dlp.PROGRESS["enabled"] = progress
        return await ytdlp_handler.download("https://youtu.be/x", self.workspace, self.settings, 1, self.cb)

    def bar_calls(self):
        return [(c[0], c[4]) for c in self.calls if c[0] is not None]      # (percent, label)

    async def test_asks_ffmpeg_for_a_progress_file(self):
        await self.run_clip([], progress=False)
        args = yt_dlp.CALLS[0]["external_downloader_args"]["ffmpeg_i"]
        self.assertEqual(args[0], "-progress")
        self.assertTrue(args[1].startswith(str(self.workspace) + "/"))     # inside the job's workspace
        self.assertFalse(list(self.workspace.glob("ffmpeg_progress_*")))   # cleaned up afterwards

    async def test_real_bar_for_video_then_audio_and_no_elapsed_text_once_it_runs(self):
        await self.run_clip(["ok"], progress=True)
        bars = self.bar_calls()
        self.assertTrue(bars, "no progress was reported")
        video = [p for p, label in bars if label == "Video"]
        audio = [p for p, label in bars if label == "Audio"]
        self.assertTrue(video and audio)
        self.assertEqual(video[-1], 100.0)
        self.assertEqual(audio[-1], 100.0)
        self.assertTrue(all(0 <= p <= 100 for p, _ in bars))
        self.assertEqual(video, sorted(video))                              # monotonic within a stream
        first_bar = next(i for i, c in enumerate(self.calls) if c[0] is not None)
        later_text = [c for c in self.calls[first_bar:] if c[0] is None and c[1] and "elapsed" not in str(c[1])
                      and c[3] is None]
        self.assertEqual(later_text, [], "elapsed-time lines kept overwriting the bar")
        # Video finishes before Audio starts
        labels_in_order = [label for _, label in bars]
        self.assertEqual(labels_in_order, sorted(labels_in_order, key=lambda l: l != "Video"))

    async def test_audio_jobs_label_their_one_stream_audio(self):
        self.settings.update(mode="audio", audio_format="mp3")
        yt_dlp.reset(["ok"])
        yt_dlp.PROGRESS.update(enabled=True, streams=1)
        await ytdlp_handler.download("https://youtu.be/x", self.workspace, self.settings, 1, self.cb)
        self.assertEqual({label for _, label in self.bar_calls()}, {"Audio"})

    async def test_falls_back_to_elapsed_time_when_ffmpeg_reports_nothing(self):
        await self.run_clip(["slow"], progress=False)
        self.assertEqual(self.bar_calls(), [])
        elapsed = [c[1] for c in self.calls if c[0] is None and c[1] and " · " in str(c[1])]
        self.assertTrue(elapsed and elapsed[0].startswith("Clip · 0:00 – 0:04 · "), elapsed)


class FfmpegProgressParsing(unittest.TestCase):
    def block(self, us, status):
        value = "N/A" if us is None else str(us)
        return f"frame=1\nout_time_us={value}\nout_time_ms={value}\nprogress={status}\n"

    def make(self, text, seconds=10):
        path = Path(tempfile.mkdtemp(prefix="ffprog-")) / "p.txt"
        path.write_text(text)
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        return ytdlp_handler._FfmpegProgress(path, seconds), path

    def test_no_file_no_data_and_not_yet_started(self):
        tracker, path = self.make("")
        self.assertIsNone(tracker.poll())
        path.write_text(self.block(None, "continue"))                  # "N/A" before the first frame
        self.assertIsNone(tracker.poll())
        tracker.path = path.parent / "missing.txt"
        self.assertIsNone(tracker.poll())

    def test_percent_from_output_time_and_capped_below_100_until_ended(self):
        tracker, path = self.make(self.block(2_500_000, "continue"))
        self.assertEqual(tracker.poll(), (0, 25.0))
        path.write_text(self.block(10_000_000, "continue"))
        self.assertEqual(tracker.poll(), (0, 99.0))                     # ffmpeg says done only via progress=end
        path.write_text(self.block(10_000_000, "end"))
        self.assertEqual(tracker.poll(), (0, 100.0))

    def test_partially_written_block_is_ignored(self):
        tracker, path = self.make(self.block(1_000_000, "continue") + "frame=2\nout_time_us=3000000\nprogress=con")
        self.assertEqual(tracker.poll(), (0, 10.0))                     # the unfinished block isn't used

    def test_next_stream_detected_by_time_going_backwards_or_restart_after_end(self):
        tracker, path = self.make(self.block(10_000_000, "end"))
        self.assertEqual(tracker.poll(), (0, 100.0))
        path.write_text(self.block(1_000_000, "continue"))              # file restarted: audio stream
        self.assertEqual(tracker.poll(), (1, 10.0))
        path.write_text(self.block(5_000_000, "continue"))
        self.assertEqual(tracker.poll(), (1, 50.0))

    def test_old_ms_key_and_end_without_a_time(self):
        tracker, path = self.make("out_time_ms=5000000\nprogress=continue\n")
        self.assertEqual(tracker.poll(), (0, 50.0))
        tracker, path = self.make("out_time_us=N/A\nprogress=end\n")
        self.assertEqual(tracker.poll(), (0, 100.0))

    def test_only_the_tail_of_a_long_log_is_read(self):
        many = "".join(self.block(i * 100_000, "continue") for i in range(1, 400))
        tracker, path = self.make(many, seconds=100)
        stream, percent = tracker.poll()
        self.assertEqual(stream, 0)
        self.assertAlmostEqual(percent, 39.9, places=1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_parses_what_the_real_ffmpeg_writes(self):
        workdir = Path(tempfile.mkdtemp(prefix="ffreal-"))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        progress = workdir / "p.txt"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-progress", str(progress), "-f", "lavfi", "-i",
                        "testsrc=d=3:s=160x120:r=25", "-f", "lavfi", "-i", "sine=d=3", "-shortest",
                        "-pix_fmt", "yuv420p", str(workdir / "o.mp4")], check=True)
        tracker = ytdlp_handler._FfmpegProgress(progress, 3)
        self.assertEqual(tracker.poll(), (0, 100.0))
