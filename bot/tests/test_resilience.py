"""A failing post-processing step (cover art, Opus conversion) must not cost the person their download."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
from downloader import ytdlp_handler  # noqa: E402
from downloader.errors import JobCancelled  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402


class Resilience(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="res-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

    async def run_download(self, script, **settings):
        yt_dlp.reset(script)
        full = {**DEFAULTS, "mode": "audio", "audio_format": "mp3", **settings}
        files = await ytdlp_handler.download("https://youtu.be/x", self.workspace, full, 1, lambda *a: None)
        return files, full

    def codec(self, call_index):
        return next(pp["preferredcodec"] for pp in yt_dlp.CALLS[call_index]["postprocessors"] if pp["key"] == "FFmpegExtractAudio")

    async def test_a_cover_art_failure_retries_without_cover_art_and_says_so(self):
        files, settings = await self.run_download(["ppfail"])
        self.assertEqual(len(files), 1)
        self.assertEqual(len(yt_dlp.CALLS), 2)
        self.assertTrue(yt_dlp.CALLS[0].get("writethumbnail"))
        self.assertFalse(yt_dlp.CALLS[1].get("writethumbnail"))
        self.assertEqual(settings["delivery_notes"], ["◧ Cover art couldn't be added this time, so the file was sent without it."])

    async def test_opus_that_still_fails_falls_back_to_mp3(self):
        files, settings = await self.run_download(["ppfail", "ppfail"], audio_format="opus")
        self.assertEqual(len(yt_dlp.CALLS), 3)
        self.assertEqual([self.codec(i) for i in range(3)], ["opus", "opus", "mp3"])
        self.assertEqual(settings["delivery_notes"], ["◧ Opus conversion failed on this server, so this one is an MP3."])
        self.assertEqual(settings["audio_format"], "opus")                         # the person's setting is untouched

    async def test_a_video_download_also_retries_without_cover_art(self):
        yt_dlp.reset(["ppfail"])
        full = dict(DEFAULTS, mode="video", quality="best")
        files = await ytdlp_handler.download("https://youtu.be/x", self.workspace, full, 1, lambda *a: None)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(full["delivery_notes"]), 1)

    async def test_if_everything_fails_the_original_error_surfaces(self):
        with self.assertRaisesRegex(Exception, "Conversion failed"):
            await self.run_download(["ppfail", "ppfail", "ppfail"], audio_format="opus")

    async def test_other_errors_are_not_retried(self):
        with self.assertRaisesRegex(Exception, "boom"):
            await self.run_download(["fail"])
        self.assertEqual(len(yt_dlp.CALLS), 1)

    async def test_a_failure_that_cannot_be_improved_by_dropping_the_cover_is_not_retried_forever(self):
        with self.assertRaises(Exception):
            await self.run_download(["ppfail", "ppfail"], embed_thumbnail=False)    # nothing to drop: no retry chain
        self.assertEqual(len(yt_dlp.CALLS), 1)

    async def test_cancelling_is_never_retried(self):
        original = ytdlp_handler._download_with_subtitles

        async def cancelled(*a, **k):
            raise JobCancelled("Cancelled by user")
        ytdlp_handler._download_with_subtitles = cancelled
        self.addCleanup(setattr, ytdlp_handler, "_download_with_subtitles", original)
        with self.assertRaises(JobCancelled):
            await self.run_download([])

    async def test_ffmpegs_real_complaint_is_logged_without_unrelated_noise(self):
        with self.assertLogs("candy.ytdlp", level="INFO") as logs:
            await self.run_download(["ppfail"])
        text = "\n".join(logs.output)
        self.assertIn("Invalid argument for option b:a", text)
        self.assertIn("ffmpeg command line", text)
        self.assertNotIn("something unrelated", text)

    async def test_the_capture_is_attached_and_verbose_without_printing(self):
        await self.run_download([])
        opts = yt_dlp.CALLS[0]
        self.assertTrue(opts["verbose"])
        self.assertTrue(hasattr(opts["logger"], "debug"))

    def test_logs_are_scrubbed_of_signed_urls_and_proxy_passwords(self):
        scrub = ytdlp_handler._scrub
        self.assertNotIn("googlevideo", scrub("GET https://rr1.googlevideo.com/videoplayback?" + "x" * 200))
        self.assertEqual(scrub("proxy socks5://user:secret@warp:1080 ok"), "proxy socks5://warp:1080 ok")
        self.assertEqual(scrub("plain text"), "plain text")


if __name__ == "__main__":
    unittest.main()
