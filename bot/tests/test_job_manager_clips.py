"""
JobManager with several files: the cache keeps all of them, "send as file"
re-sends all of them, and the buttons make sense on a multi-clip delivery.
"""
import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

from telegram.error import TelegramError  # noqa: E402  (the stub)

from jobqueue import job_manager as jm  # noqa: E402

RID, URL = "rid0000002", "https://www.youtube.com/watch?v=abc"


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.deleted: list = []
        self.fail_on_document: int | None = None    # 1-based index of the send_document call to fail

    def _record(self, kind, kw, file):
        self.sent.append({"kind": kind, "caption": kw.get("caption", ""), "markup": kw.get("reply_markup"),
                          "filename": getattr(file, "filename", None)})

    async def send_video(self, chat_id, file, **kw):
        self._record("video", kw, file)

    async def send_audio(self, chat_id, file, **kw):
        self._record("audio", kw, file)

    async def send_document(self, chat_id, file, **kw):
        count = sum(1 for s in self.sent if s["kind"] == "document") + 1
        if self.fail_on_document == count:
            raise TelegramError("boom")
        self._record("document", kw, file)

    async def send_photo(self, chat_id, file, **kw):
        self._record("photo", kw, file)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_message_text(self, *a, **kw):
        pass

    async def edit_message_caption(self, *a, **kw):
        pass


def labels(markup):
    return [b.text for b in markup.buttons()] if markup else []


class MultiFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jmtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "cache").mkdir()
        counter = iter(range(1000))
        original = jm.new_cache_path
        jm.new_cache_path = lambda suffix: self.tmp / "cache" / f"c{next(counter)}{suffix}"
        self.addCleanup(setattr, jm, "new_cache_path", original)

        self.bot = FakeBot()
        self.manager = jm.JobManager(self.bot, max_concurrent=1)

    def make_files(self, *names) -> list[Path]:
        files = []
        for name in names:
            path = self.tmp / name
            path.write_bytes(b"x" * 1000)
            files.append(path)
        return files

    def make_job(self, **settings) -> jm.Job:
        return jm.Job(rid=RID, user_id=1, chat_id=7, url=URL, settings=settings, status_message_id=55,
                      is_photo=False, header="Sending")

    # -- cache
    async def test_every_file_is_cached_not_just_the_last(self):
        for f in self.make_files("a [clip1].mp4", "b [clip2].mp4", "c [clip3].mp4"):
            await self.manager._cache_video_file(RID, URL, f)
        self.assertEqual(len(self.manager._recent_files[RID]), 3)
        self.assertTrue(self.manager.has_cached_video(RID, URL))

    async def test_resend_sends_all_as_documents_with_the_link_only_on_the_last(self):
        for f in self.make_files("one.mp4", "two.mp4"):
            await self.manager._cache_video_file(RID, URL, f)
        self.assertTrue(await self.manager.send_cached_as_document(RID, 7, URL, 55))

        self.assertEqual([s["kind"] for s in self.bot.sent], ["document", "document"])
        self.assertEqual([s["filename"] for s in self.bot.sent], ["one.mp4", "two.mp4"])
        self.assertIsNone(self.bot.sent[0]["markup"])
        self.assertIsNotNone(self.bot.sent[1]["markup"])
        self.assertEqual(self.bot.deleted, [55])

    async def test_a_missing_cached_file_means_re_download(self):
        files = self.make_files("one.mp4", "two.mp4")
        for f in files:
            await self.manager._cache_video_file(RID, URL, f)
        self.manager._recent_files[RID][1]["path"].unlink()
        self.assertFalse(self.manager.has_cached_video(RID, URL))
        self.assertFalse(await self.manager.send_cached_as_document(RID, 7, URL, 55))
        self.assertEqual(self.bot.sent, [])

    async def test_failure_after_the_first_file_does_not_trigger_a_duplicating_re_download(self):
        for f in self.make_files("one.mp4", "two.mp4"):
            await self.manager._cache_video_file(RID, URL, f)
        self.bot.fail_on_document = 2
        self.assertTrue(await self.manager.send_cached_as_document(RID, 7, URL, 55))   # not False
        self.assertEqual(len(self.bot.sent), 1)

    async def test_failure_of_the_very_first_file_falls_back_to_re_download(self):
        for f in self.make_files("one.mp4", "two.mp4"):
            await self.manager._cache_video_file(RID, URL, f)
        self.bot.fail_on_document = 1
        self.assertFalse(await self.manager.send_cached_as_document(RID, 7, URL, 55))

    async def test_drop_cached_deletes_the_files(self):
        await self.manager._cache_video_file(RID, URL, self.make_files("one.mp4")[0])
        path = self.manager._recent_files[RID][0]["path"]
        self.manager._drop_cached(RID)
        self.assertFalse(path.exists())
        self.assertNotIn(RID, self.manager._recent_files)

    async def test_expiry_removes_only_the_expired_file(self):
        await self.manager._cache_video_file(RID, URL, self.make_files("one.mp4")[0])
        await self.manager._cache_video_file(RID, URL, self.make_files("two.mp4")[0])
        first = self.manager._recent_files[RID][0]["path"]
        original = jm.RECENT_FILE_TTL_SECONDS
        jm.RECENT_FILE_TTL_SECONDS = 0
        self.addCleanup(setattr, jm, "RECENT_FILE_TTL_SECONDS", original)
        await self.manager._expire_cached_file(RID, first)
        self.assertFalse(first.exists())
        self.assertEqual(len(self.manager._recent_files[RID]), 1)

    # -- delivery buttons
    async def test_single_video_keeps_the_original_button_and_wording(self):
        await self.manager._send_files(self.make_job(mode="video"), self.make_files("solo.mp4"))
        sent = self.bot.sent[0]
        self.assertIn("▤ Send as file instead", labels(sent["markup"]))
        self.assertIn("compressed preview", sent["caption"])

    async def test_several_videos_put_the_all_button_on_the_last_only(self):
        await self.manager._send_files(self.make_job(mode="video", sections=[(0, 5), (9, 12)]),
                                       self.make_files("a.mp4", "b.mp4"))
        first, last = self.bot.sent
        self.assertIsNone(first["markup"])
        self.assertNotIn("Want the original", first["caption"])
        self.assertIn("▤ Send all as files", labels(last["markup"]))
        self.assertIn("original files", last["caption"])
        self.assertEqual(self.bot.deleted, [55])          # status message removed once, at the end

    async def test_several_audio_clips_behave_the_same_way(self):
        await self.manager._send_files(self.make_job(mode="audio"), self.make_files("a.mp3", "b.mp3"))
        first, last = self.bot.sent
        self.assertEqual([first["kind"], last["kind"]], ["audio", "audio"])
        self.assertIsNone(first["markup"])
        self.assertIn("▤ Send all as files", labels(last["markup"]))

    async def test_forced_documents_put_the_link_on_the_last_only(self):
        job = self.make_job(mode="video")
        job.force_document = True
        await self.manager._send_files(job, self.make_files("a.mp4", "b.mp4"))
        self.assertIsNone(self.bot.sent[0]["markup"])
        self.assertIsNotNone(self.bot.sent[1]["markup"])

    # -- info line and history
    def test_info_line_and_history_describe_clips(self):
        job = self.make_job(mode="video", quality="best", sections=[(0, 5), (9, 12)], sections_merge=True)
        job.info = {"type": "Video", "site": "Youtube"}
        self.assertIn("✂ 2 clips merged", self.manager._info_line(job))
        job.settings["sections_merge"] = False
        self.assertIn("✂ 2 clips", self.manager._info_line(job))
        self.assertNotIn("merged", self.manager._info_line(job))
        job.settings["sections"] = [(0, 5)]
        self.assertIn("✂ 1 clip", self.manager._info_line(job))
        self.assertEqual(self.manager._history_quality(job.settings), "best ✂1")
        audio = {"mode": "audio", "audio_format": "mp3", "sections": [(0, 5), (9, 12), (20, 30)]}
        self.assertEqual(self.manager._history_quality(audio), "mp3 ✂3")
        self.assertEqual(self.manager._history_quality({"mode": "video", "quality": "720p"}), "720p")

    async def test_run_job_caches_all_clips_and_names_history_without_the_range(self):
        files = self.make_files("My title [00-00-00–00-00-05].mp4", "My title [00-00-09–00-00-12].mp4")

        async def fake_download(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            return files

        @contextlib.contextmanager
        def fake_workspace():
            yield self.tmp

        original = (jm.dispatch_download, jm.job_workspace)
        jm.dispatch_download, jm.job_workspace = fake_download, fake_workspace
        self.addCleanup(lambda: setattr(jm, "dispatch_download", original[0]))
        self.addCleanup(lambda: setattr(jm, "job_workspace", original[1]))

        job = self.make_job(mode="video", quality="best", sections=[(0, 5), (9, 12)])
        await self.manager._run_job(job)

        self.assertEqual(len(self.manager._recent_files[RID]), 2)
        self.assertEqual(job.title, "My title")
        self.assertEqual([s["kind"] for s in self.bot.sent], ["video", "video"])

        # A second run of the same job (Try again) replaces the cache instead of piling up
        await self.manager._run_job(job)
        self.assertEqual(len(self.manager._recent_files[RID]), 2)


if __name__ == "__main__":
    unittest.main()
