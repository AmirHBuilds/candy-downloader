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
from downloader.probe import ProbeResult, _chapters, _fill_missing_sizes  # noqa: E402
from downloader.sizes import describe_best  # noqa: E402
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

    def test_the_main_settings_menu_is_tidy_two_per_line_and_has_no_history(self):
        menu = settings_menu.main_menu({})
        rows = [[b.text for b in row] for row in menu.inline_keyboard]
        self.assertEqual(rows, [["🧠 ADHD Mode: off"], ["🎨 Appearance", "⚙️ Advanced"], ["🔑 Cookies", "↻ Reset"],
                                ["← Back to start"]])
        self.assertFalse([d for d in (b.callback_data for b in menu.buttons()) if "history" in d])
        self.assertEqual([b.text for b in settings_menu.main_menu({"adhd_mode": True}).buttons()][0], "🧠⚡ ADHD Mode: ON")

    def test_appearance_holds_the_sizes_toggle_and_the_bar_style_side_by_side(self):
        on = settings_menu.look_menu({"show_sizes": True})
        off = settings_menu.look_menu({"show_sizes": False})
        self.assertEqual([[b.text for b in row] for row in on.inline_keyboard], [["📏 Sizes: ON", "🎨 Bar style"], ["← Back"]])
        self.assertEqual(on.inline_keyboard[0][0].callback_data, "s|show_sizes|0")
        self.assertEqual(off.inline_keyboard[0][0].callback_data, "s|show_sizes|1")
        self.assertEqual(on.inline_keyboard[0][1].callback_data, "nav|bars")
        self.assertIn("📏 Sizes: ON", [b.text for b in settings_menu.look_menu({}).buttons()])         # default: on

    def test_appearance_text_names_the_current_choices(self):
        text = settings_menu.look_title({"show_sizes": False, "bar_style": "moon"})
        self.assertIn("Sizes on buttons: <b>off</b>", text)
        self.assertIn("Progress bar: <b>Moons</b>", text)
        self.assertIn("<b>Auto</b>", settings_menu.look_title({}))

    def test_the_bar_picker_goes_back_to_appearance(self):
        self.assertEqual(settings_menu.bars_menu({}).inline_keyboard[-1][0].callback_data, "nav|look")

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



class SettingsNavigation(unittest.IsolatedAsyncioTestCase):
    """The real settings_callback: Appearance opens, and toggling sizes stays there."""

    class Query:
        def __init__(self, data):
            self.data, self.edits = data, []
            self.message = type("M", (), {"chat_id": 1, "message_id": 2})()

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **kw):
            self.edits.append((text, kw.get("reply_markup")))

    def setUp(self):
        self.stored = dict(DEFAULTS)
        async def allow(*a, **k):
            return True
        for name, value in (("gate_callback", allow), ("get_settings", lambda uid: dict(self.stored)),
                            ("update_setting", lambda uid, key, value: self.stored.__setitem__(key, value)),
                            ("pending_setting_input", {}), ("pending_section_input", {})):
            self.addCleanup(setattr, main, name, getattr(main, name))
            setattr(main, name, value)

    async def press(self, data):
        query = self.Query(data)
        update = type("U", (), {"callback_query": query, "effective_user": type("X", (), {"id": 1})()})()
        await main.settings_callback(update, None)
        return query.edits[-1]

    async def test_appearance_opens_and_back_returns_to_settings(self):
        text, markup = await self.press("nav|look")
        self.assertIn("Appearance", text)
        text, markup = await self.press("nav|main")
        self.assertIn("Your settings", text)

    async def test_toggling_sizes_keeps_you_on_appearance(self):
        text, markup = await self.press("s|show_sizes|0")
        self.assertFalse(self.stored["show_sizes"])
        self.assertIn("Appearance", text)
        self.assertIn("📏 Sizes: off", [b.text for b in markup.buttons()])

    async def test_choosing_a_bar_style_stays_on_the_picker(self):
        text, markup = await self.press("s|bar_style|moon")
        self.assertEqual(self.stored["bar_style"], "moon")
        self.assertIn("Progress bar style", text)

    async def test_adhd_toggle_still_returns_to_the_main_settings(self):
        text, markup = await self.press("s|adhd_mode|1")
        self.assertIn("Your settings", text)


class SizesForSitesThatAnnounceNone(unittest.TestCase):
    """Instagram announces no filesize and no bitrate: ask the server instead."""

    def setUp(self):
        self.asked: list[str] = []
        self.sizes: dict[str, int | None] = {}
        original = probe_module._head_size

        def fake(url, headers):
            self.asked.append(url)
            return self.sizes.get(url)
        probe_module._head_size = fake
        self.addCleanup(setattr, probe_module, "_head_size", original)

    def fmt(self, name, height=None, audio_only=False, **extra):
        base = {"format_id": name, "url": f"https://cdn.example/{name}", "protocol": "https"}
        if audio_only:
            return {**base, "vcodec": "none", "acodec": "mp4a", **extra}
        return {**base, "height": height, "vcodec": "avc1", "acodec": "mp4a" if extra.pop("muxed", False) else "none", **extra}

    def test_sizes_are_filled_from_the_server_for_the_best_format_of_each_height(self):
        formats = [self.fmt("v1080", 1080), self.fmt("v1080b", 1080), self.fmt("v720", 720), self.fmt("a", audio_only=True)]
        self.sizes = {"https://cdn.example/v1080": 30 * MB, "https://cdn.example/v720": 15 * MB, "https://cdn.example/a": 2 * MB}
        info = {"formats": formats}
        _fill_missing_sizes(info)
        self.assertEqual(formats[0].get("filesize"), 30 * MB)
        self.assertEqual(len(self.asked), 3)                                       # not both 1080p copies
        from downloader.sizes import estimate_sizes
        self.assertEqual(estimate_sizes(formats, 60, [1080, 720])["1080"], 32 * MB)

    def test_a_site_that_announces_sizes_is_never_asked(self):
        _fill_missing_sizes({"formats": [self.fmt("v", 720, filesize=5 * MB), self.fmt("w", 480)]})
        _fill_missing_sizes({"formats": [self.fmt("v", 720, tbr=900)]})
        self.assertEqual(self.asked, [])

    def test_manifests_are_skipped_and_failures_are_harmless(self):
        formats = [{**self.fmt("hls", 720), "protocol": "m3u8_native"}, self.fmt("v", 480)]
        _fill_missing_sizes({"formats": formats})
        self.assertEqual(self.asked, ["https://cdn.example/v"])                      # a stream manifest has no single size
        self.assertNotIn("filesize", formats[1])                                     # the server said nothing: untouched
        _fill_missing_sizes({"formats": []})
        _fill_missing_sizes({})

    def test_the_number_of_requests_is_capped(self):
        _fill_missing_sizes({"formats": [self.fmt(f"v{h}", h) for h in range(100, 1300, 100)]})
        self.assertLessEqual(len(self.asked), probe_module.HEAD_MAX_FORMATS + 1)


class ExplainingTheEstimate(unittest.TestCase):
    def test_the_log_line_names_the_formats_and_how_much_to_trust_each_number(self):
        formats = [
            {"format_id": "137", "height": 1080, "vcodec": "avc1.64", "acodec": "none", "filesize": 700 * MB},
            {"format_id": "299", "height": 1080, "vcodec": "vp09", "acodec": "none", "filesize_approx": 2000 * MB},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 130},
        ]
        text = describe_best(formats, 3600)
        self.assertIn("137 avc1 1080p 700MB (exact)", text)
        self.assertIn("140 mp4a", text)
        self.assertIn("(bitrate)", text)
        self.assertNotIn("299", text)                                                # the VP9 one is not what would be fetched
        self.assertEqual(describe_best([], 60), "nothing")


if __name__ == "__main__":
    unittest.main()
