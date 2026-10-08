"""
Things reported after real use on 2026-10-01:
  * the preview failing with an empty reason (a timeout) even though downloading works
  * the progress bar moving backwards, and "100% ... then downloading again"
  * a final "Didn't work" message being replaced by a stale "One moment…"
"""
import asyncio
import contextlib
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
from downloader import probe as probe_module, ytdlp_handler  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import messages  # noqa: E402

RID, URL = "rid0000003", "https://www.youtube.com/watch?v=abc"


# ---------------------------------------------------------------- preview
class ProbeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        yt_dlp.reset([])
        for name, value in (("get_settings", lambda uid: {}), ("cookie_file_for", lambda uid, s: None)):
            self.addCleanup(setattr, probe_module, name, getattr(probe_module, name))
            setattr(probe_module, name, value)

    async def test_a_slow_preview_reports_a_timeout_instead_of_an_empty_failure(self):
        yt_dlp.EXTRACT["delay"] = 0.5
        self.addCleanup(setattr, probe_module, "PROBE_TIMEOUT", probe_module.PROBE_TIMEOUT)
        probe_module.PROBE_TIMEOUT = 0.05
        with self.assertLogs("candy.probe", level="INFO") as logs:
            result = await probe_module.probe(URL, user_id=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timed out")
        self.assertIn("timed out", "\n".join(logs.output))          # the log line says WHY now

    async def test_other_failures_log_the_exception_type(self):
        def boom(*a, **k):
            raise ValueError("")                                      # an exception with an empty message
        original = yt_dlp.YoutubeDL.extract_info
        yt_dlp.YoutubeDL.extract_info = boom
        self.addCleanup(setattr, yt_dlp.YoutubeDL, "extract_info", original)
        with self.assertLogs("candy.probe", level="INFO") as logs:
            result = await probe_module.probe(URL, user_id=1)
        self.assertFalse(result.ok)
        self.assertIn("ValueError", "\n".join(logs.output))

    async def test_a_normal_preview_still_works(self):
        yt_dlp.EXTRACT["result"] = {"title": "Hello", "duration": 61, "formats": [
            {"height": 720, "vcodec": "avc1", "acodec": "none"}, {"vcodec": "none", "acodec": "mp4a"}]}
        result = await probe_module.probe(URL, user_id=1)
        self.assertTrue(result.ok)
        self.assertEqual((result.title, result.heights, result.duration), ("Hello", [720], 61))

    def test_timeouts_allow_for_a_slow_connection(self):
        self.assertGreaterEqual(probe_module.PROBE_TIMEOUT, 30)
        self.assertGreaterEqual(probe_module.PROBE_TIMEOUT_WITH_COOKIES, 60)

    def test_timeout_note_is_friendly(self):
        self.assertIn("took too long", messages.preview_failed_note("timed out"))
        self.assertIn("login", messages.preview_failed_note("Sign in to confirm"))
        self.assertEqual(messages.preview_failed_reason(""), "")
        self.assertEqual(messages.preview_failed_reason("ERROR: [youtube] abc-DEF_1: Sign in <now>"),
                         "\n<i>Sign in &lt;now&gt;</i>")
        self.assertLessEqual(len(messages.preview_failed_reason("x" * 500)), 150)


# ---------------------------------------------------------------- progress bars
def video_event(status, done, total=800):
    return {"status": status, "downloaded_bytes": done, "total_bytes": total, "filename": "v.f137.mp4",
            "info_dict": {"format_id": "137", "vcodec": "avc1", "acodec": "none",
                          "requested_formats": [{"filesize": 800}, {"filesize": 200}]}}


def audio_event(status, done, total=200):
    return {"status": status, "downloaded_bytes": done, "total_bytes": total, "filename": "a.f140.m4a",
            "info_dict": {"format_id": "140", "vcodec": "none", "acodec": "mp4a",
                          "requested_formats": [{"filesize": 800}, {"filesize": 200}]}}


class ProgressBarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="bartest-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.calls: list[tuple] = []

    async def run_download(self, events, **settings):
        yt_dlp.reset([])
        yt_dlp.HOOK_EVENTS.extend(events)
        full = dict(DEFAULTS, mode="video", quality="best", **settings)
        await ytdlp_handler.download(URL, self.workspace, full, 1, lambda *a: self.calls.append(a))
        await asyncio.sleep(0)
        return [(c[0], c[4]) for c in self.calls if c[0] is not None]       # (percent, label)

    STREAMS = [video_event("downloading", 200), video_event("downloading", 400), video_event("downloading", 800),
               video_event("finished", 800),
               audio_event("downloading", 50), audio_event("downloading", 150), audio_event("finished", 200)]

    async def test_adhd_mode_gets_one_continuous_bar_across_video_and_audio(self):
        bar = [p for p, _ in await self.run_download(self.STREAMS, adhd_mode=True)]
        self.assertEqual(bar, sorted(bar), f"bar went backwards: {bar}")      # never restarts
        self.assertEqual(bar[-1], 100.0)
        self.assertTrue(all(p < 100 for p in bar[:-1]), f"hit 100 before the end: {bar}")
        after_video = bar[3]                                                  # the video stream just finished
        self.assertAlmostEqual(after_video, 80.0, delta=1)                    # 800 of 1000 bytes, not 100

    async def test_normal_mode_keeps_separate_labelled_streams(self):
        bar = await self.run_download(self.STREAMS, adhd_mode=False)
        video = [p for p, label in bar if label == "Video"]
        audio = [p for p, label in bar if label == "Audio"]
        self.assertEqual(video[-1], 100.0)
        self.assertEqual(audio[-1], 100.0)
        self.assertLess(audio[0], 50)                                         # audio has its own 0-100 run

    async def test_a_bar_never_moves_backwards_when_a_tick_has_no_total(self):
        events = [video_event("downloading", 400), video_event("downloading", 450, total=None),
                  video_event("downloading", 500)]
        for e in events:
            e["info_dict"].pop("requested_formats")
        bar = [p for p, _ in await self.run_download(events, adhd_mode=False)]
        self.assertEqual(bar, sorted(bar), bar)
        self.assertEqual(bar[1], 50.0)                                        # held, instead of dropping to 0

    async def test_adhd_without_announced_sizes_falls_back_to_per_stream_without_crashing(self):
        events = [video_event("downloading", 400), video_event("finished", 800)]
        for e in events:
            e["info_dict"]["requested_formats"] = [{}, {}]
        bar = [p for p, _ in await self.run_download(events, adhd_mode=True)]
        self.assertEqual(bar[-1], 100.0)

    async def test_a_single_combined_stream_finishes_at_100(self):
        events = [video_event("downloading", 400), video_event("finished", 800)]
        for e in events:
            e["info_dict"]["requested_formats"] = [{"filesize": 800}]
        bar = [p for p, _ in await self.run_download(events, adhd_mode=True)]
        self.assertEqual(bar[-1], 100.0)

    def test_postprocessors_use_yt_dlps_real_names(self):
        label = ytdlp_handler._postprocessor_label
        self.assertEqual(label("Metadata"), "Adding metadata")
        self.assertEqual(label("MoveFiles"), "Moving file")
        self.assertEqual(label("ExtractAudio"), "Extracting audio")
        self.assertEqual(label("FFmpegMetadata"), "Adding metadata")          # long spelling still works
        self.assertEqual(label("FixupM4a"), "Fixing up file")
        self.assertEqual(label("SomethingNew"), "SomethingNew")


# ---------------------------------------------------------------- status message ordering
class FakeBot:
    def __init__(self):
        self.shown: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.delays: list[float] = []         # per call: how long the "network" takes

    async def edit_message_text(self, text, **kw):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        delay = self.delays.pop(0) if self.delays else 0.0
        await asyncio.sleep(delay)
        self.shown.append(text)
        self.in_flight -= 1

    edit_message_caption = edit_message_text

    async def delete_message(self, *a, **k):
        pass


def make_job(**settings):
    return jm.Job(rid=RID, user_id=1, chat_id=7, url=URL, settings=settings, status_message_id=55,
                  is_photo=False, header="Downloading")


class EditOrderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.manager = jm.JobManager(self.bot, max_concurrent=1)

    async def test_updates_arrive_in_order_even_when_the_network_is_out_of_order(self):
        """Earlier requests take LONGER, so sent concurrently they would finish last."""
        job = make_job()
        self.bot.delays = [0.16, 0.12, 0.08, 0.04, 0.0, 0.0]
        await asyncio.gather(*(self.manager._emit(job, f"Step {i}", push=False) for i in range(1, 7)))
        self.assertIn("Step 6", self.bot.shown[-1])                           # the newest is what stays on screen
        self.assertEqual(self.bot.max_in_flight, 1)                           # one request at a time

    async def test_a_burst_is_collapsed_to_the_newest_text(self):
        job = make_job()
        await asyncio.gather(*(self.manager._emit(job, f"Step {i}", push=False) for i in range(1, 30)))
        self.assertLess(len(self.bot.shown), 29)                              # intermediate ticks were skipped
        self.assertIn("Step 29", self.bot.shown[-1])

    async def test_the_sender_recovers_after_an_error(self):
        job = make_job()
        calls = []

        async def flaky(text, **kw):
            calls.append(text)
            if len(calls) == 1:
                raise jm.TelegramError("message is not modified")

        self.bot.edit_message_text = flaky
        await self.manager._emit(job, "one", push=False)
        await self.manager._emit(job, "two", push=False)
        self.assertEqual(len(calls), 2)
        self.assertFalse(job.editing)

    async def test_progress_that_runs_after_the_job_finished_is_dropped(self):
        job = make_job(adhd_mode=True)
        job.steps = ["final message"]
        job.terminal = True
        await self.manager._emit_progress(job, "One moment…", False, None)
        self.assertEqual(job.steps, ["final message"])
        self.assertEqual(self.bot.shown, [])

    async def test_a_late_progress_tick_cannot_replace_the_failure_message(self):
        """The reported bug: "✕ Didn't work" followed by "One moment…"."""
        holder = {}

        async def failing_download(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            holder["progress_cb"] = progress_cb
            raise RuntimeError("boom")

        original = (jm.dispatch_download, jm.job_workspace)
        jm.dispatch_download = failing_download
        jm.job_workspace = lambda: contextlib.nullcontext(Path(tempfile.mkdtemp()))
        self.addCleanup(lambda: setattr(jm, "dispatch_download", original[0]))
        self.addCleanup(lambda: setattr(jm, "job_workspace", original[1]))

        self.manager.start()
        self.addCleanup(lambda: [t.cancel() for t in self.manager._workers])
        await self.manager.enqueue(RID, 1, 7, URL, dict(DEFAULTS, mode="video", adhd_mode=True), 55)
        for _ in range(200):                                                  # wait for the failure to be shown
            await asyncio.sleep(0.01)
            if self.bot.shown and "Didn't work" in self.bot.shown[-1]:
                break
        self.assertIn("Didn't work", self.bot.shown[-1])

        holder["progress_cb"]("ytdlp", None, None, None, "Merging video & audio", None, None)   # a late stage tick
        await asyncio.sleep(0.1)
        self.assertIn("Didn't work", self.bot.shown[-1])
        self.assertNotIn("One moment", self.bot.shown[-1])


if __name__ == "__main__":
    unittest.main()
