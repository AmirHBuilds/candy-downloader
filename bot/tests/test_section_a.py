"""Sizes on buttons, chapters from the probe, and the cookie warning through a real job."""
import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401

import yt_dlp  # noqa: E402  (the stub)
import main  # noqa: E402
from downloader import cookie_health, probe as probe_module  # noqa: E402
from downloader.probe import ProbeResult, _chapters  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import quick_menu, settings_menu  # noqa: E402

RID, URL, USER = "rid0000004", "https://www.youtube.com/watch?v=abc", 42
MB = 1_000_000


def labels(markup):
    return [b.text for b in markup.buttons()]


# ---------------------------------------------------------------- probe data
class ProbeDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        yt_dlp.reset([])
        for name, value in (("get_settings", lambda uid: {}), ("cookie_file_for", lambda uid, s: None)):
            self.addCleanup(setattr, probe_module, name, getattr(probe_module, name))
            setattr(probe_module, name, value)

    async def test_probe_returns_estimates_and_chapters(self):
        yt_dlp.EXTRACT["result"] = {
            "title": "T", "duration": 3600,
            "formats": [{"height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 400 * MB},
                        {"vcodec": "none", "acodec": "mp4a", "filesize": 60 * MB}],
            "chapters": [{"title": "Intro", "start_time": 0, "end_time": 60},
                         {"title": "Main", "start_time": 60, "end_time": 3600.4}],
        }
        result = await probe_module.probe(URL, user_id=1)
        self.assertEqual(result.sizes["720"], 460 * MB)
        self.assertEqual(result.chapters, [("Intro", 0.0, 60.0), ("Main", 60.0, 3600.0)])   # end clamped to the length

    def test_chapter_cleanup(self):
        info = {"chapters": [
            {"title": "ok", "start_time": 0, "end_time": 10},
            {"title": "no end", "start_time": 10},                       # end defaults to the video length
            {"title": "tiny", "start_time": 20, "end_time": 20.5},       # under a second: dropped
            {"start_time": 30, "end_time": 40},                          # untitled
            {"title": "junk", "start_time": "x", "end_time": 5},         # unparseable: dropped
            {"title": "t" * 200, "start_time": 50, "end_time": 60},
        ]}
        got = _chapters(info, 100)
        self.assertEqual([c[0][:6] for c in got], ["ok", "no end", "Chapte", "tttttt"])
        self.assertEqual(got[1], ("no end", 10.0, 100.0))
        self.assertEqual(len(got[3][0]), 80)
        self.assertEqual(_chapters({}, 100), [])
        self.assertEqual(len(_chapters({"chapters": [{"title": str(i), "start_time": i * 2, "end_time": i * 2 + 1}
                                                    for i in range(500)]}, None)), 200)

    async def test_a_strange_format_list_never_breaks_the_preview(self):
        yt_dlp.EXTRACT["result"] = {"title": "T", "duration": 60, "formats": [{"height": "tall", "vcodec": "avc1",
                                                                                "acodec": "none", "filesize": "lots"}]}
        original = probe_module.estimate_sizes
        probe_module.estimate_sizes = lambda *a, **k: 1 / 0
        self.addCleanup(setattr, probe_module, "estimate_sizes", original)
        result = await probe_module.probe(URL, user_id=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.sizes, {})


# ---------------------------------------------------------------- sizes on the menus
PROBE = ProbeResult(ok=True, title="T", heights=[1080, 720, 480], has_audio=True, duration=3600,
                    sizes={"best": 900 * MB, "1080": 900 * MB, "720": 500 * MB, "480": 250 * MB,
                           "worst": 100 * MB, "mp3": 86 * MB, "opus": 60 * MB})


class SizeButtonTests(unittest.TestCase):
    def setUp(self):
        for name in ("pending_probes", "pending_sections"):
            setattr(main, name, {})
        main.pending_probes[RID] = PROBE
        self.settings = dict(DEFAULTS)
        main.get_settings = lambda uid: self.settings

    def test_the_setting_defaults_to_on_and_labels_show_up_on_every_button(self):
        self.assertTrue(DEFAULTS["show_sizes"])
        menu = main._main_menu_for(RID, PROBE, USER)
        self.assertIn("★ Best available ~900MB", labels(menu))
        self.assertIn("720p ~500MB", labels(menu))
        self.assertIn("♪ MP3 ~86MB", labels(menu))
        self.assertIn("♪ Opus ~60MB", labels(menu))
        more = main._more_menu_for(RID, PROBE, USER)
        self.assertIn("↓ Smallest size ~100MB", labels(more))
        self.assertIn("480p ~250MB", labels(more))

    def test_turning_the_setting_off_removes_every_size(self):
        self.settings["show_sizes"] = False
        for menu in (main._main_menu_for(RID, PROBE, USER), main._more_menu_for(RID, PROBE, USER)):
            self.assertFalse([l for l in labels(menu) if "~" in l], labels(menu))
        self.assertIn("★ Best available", labels(main._main_menu_for(RID, PROBE, USER)))

    def test_sizes_shrink_to_the_chosen_sections(self):
        from downloader.sections import SectionDraft
        draft = SectionDraft()
        draft.set_value(0, "end", 360, 3600)                      # 6 of 60 minutes = 10%
        main.pending_sections[RID] = draft
        self.assertIn("720p ~50MB", labels(main._main_menu_for(RID, PROBE, USER)))
        self.assertIn("♪ MP3 ~8.6MB", labels(main._main_menu_for(RID, PROBE, USER)))

    def test_no_estimates_means_plain_buttons(self):
        bare = ProbeResult(ok=True, heights=[720], has_audio=True, duration=60)
        self.assertEqual(main._size_labels(RID, bare, USER), {})
        self.assertIn("720p", labels(quick_menu.video_menu(bare, RID)))

    def test_audio_only_sources_get_sizes_too(self):
        audio_only = ProbeResult(ok=True, heights=[], has_audio=True, duration=3600, sizes={"mp3": 86 * MB, "opus": 60 * MB})
        main.pending_probes[RID] = audio_only
        self.assertIn("♪ MP3 ~86MB", labels(main._main_menu_for(RID, audio_only, USER)))

    def test_the_settings_screen_has_the_toggle(self):
        on = settings_menu.main_menu({"show_sizes": True})
        off = settings_menu.main_menu({"show_sizes": False})
        data = lambda m: {b.text: b.callback_data for b in m.buttons()}
        self.assertEqual(data(on)["📏 Sizes on buttons: ON"], "s|show_sizes|0")
        self.assertEqual(data(off)["📏 Sizes on buttons: off"], "s|show_sizes|1")
        self.assertIn("📏 Sizes on buttons: ON", data(settings_menu.main_menu({})))        # default: on

    def test_the_callback_accepts_the_new_key(self):
        import inspect
        self.assertIn('"show_sizes"', inspect.getsource(main.settings_callback))


# ---------------------------------------------------------------- the cookie warning, end to end
class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat": chat_id, "text": text})

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)

    edit_message_caption = edit_message_text

    async def delete_message(self, *a, **k):
        pass


class CookieAlertJobTests(unittest.IsolatedAsyncioTestCase):
    BOT_CHECK = "Sign in to confirm you’re not a bot"

    def setUp(self):
        self.bot = FakeBot()
        self.manager = jm.JobManager(self.bot, max_concurrent=1)
        self.fresh_watch = cookie_health.CookieWatch(threshold=2, cooldown_seconds=3600)
        self.patches = [(cookie_health, "watch", self.fresh_watch),
                        (cookie_health, "cookie_file_for", lambda uid, s: "/x/c.txt" if s.get("cookies_enabled") else None)]
        for module, name, value in self.patches:
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)
        self.outcome = {"error": self.BOT_CHECK}

        async def fake_download(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            if self.outcome["error"]:
                raise RuntimeError(self.outcome["error"])
            return []

        original = (jm.dispatch_download, jm.job_workspace)
        jm.dispatch_download = fake_download
        jm.job_workspace = lambda: contextlib.nullcontext(Path(tempfile.mkdtemp()))
        self.addCleanup(lambda: (setattr(jm, "dispatch_download", original[0]), setattr(jm, "job_workspace", original[1])))

    def job(self, url=URL, cookies=True):
        return jm.Job(rid=RID, user_id=USER, chat_id=7, url=url, settings={"mode": "video", "cookies_enabled": cookies},
                      status_message_id=55, is_photo=False, header="Queued")

    async def run_once(self, job):
        """What the worker loop does for one job, using the real loop."""
        self.manager._queue.put_nowait(job)
        worker = asyncio.create_task(self.manager._worker_loop(0))
        await asyncio.wait_for(self.manager._queue.join(), 5)
        worker.cancel()

    async def test_the_second_sign_in_failure_warns_once_in_its_own_message(self):
        await self.run_once(self.job())
        self.assertEqual(self.bot.sent, [])                                  # one failure: no warning yet
        await self.run_once(self.job())
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.sent[0]["chat"], 7)
        self.assertIn("/cookies", self.bot.sent[0]["text"])
        await self.run_once(self.job())
        self.assertEqual(len(self.bot.sent), 1)                              # cooldown: no repeat

    async def test_an_explicit_message_warns_immediately(self):
        self.outcome["error"] = "The provided YouTube account cookies are no longer valid"
        await self.run_once(self.job())
        self.assertEqual(len(self.bot.sent), 1)

    async def test_a_success_resets_the_count(self):
        await self.run_once(self.job())
        self.outcome["error"] = None
        await self.run_once(self.job())                                      # worked
        self.outcome["error"] = self.BOT_CHECK
        await self.run_once(self.job())
        self.assertEqual(self.bot.sent, [])

    async def test_nothing_without_cookies_or_on_other_sites_or_for_other_errors(self):
        await self.run_once(self.job(cookies=False))
        await self.run_once(self.job(cookies=False))
        await self.run_once(self.job(url="https://vimeo.com/1"))
        await self.run_once(self.job(url="https://vimeo.com/1"))
        self.outcome["error"] = "HTTP Error 500"
        await self.run_once(self.job())
        await self.run_once(self.job())
        self.assertEqual(self.bot.sent, [])

    async def test_a_failing_warning_does_not_break_the_job_loop(self):
        async def boom(*a, **k):
            raise jm.TelegramError("blocked by user")
        self.bot.send_message = boom
        self.outcome["error"] = "The provided YouTube account cookies are no longer valid"
        await self.run_once(self.job())                                      # must simply finish


if __name__ == "__main__":
    unittest.main()
