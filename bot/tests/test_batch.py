"""
Playlists and several links at once: listing, the picker, and a real JobManager
running the items into ONE shared status message (with a fake downloader).
"""
import asyncio
import contextlib
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
import main  # noqa: E402
from downloader import dispatcher, playlist as playlist_module, probe as probe_module  # noqa: E402
from downloader.playlist import (  # noqa: E402
    Entry, ListingError, entries_from_info, entry_url, list_playlist, looks_like_playlist, quick_titles, short_label,
    wants_single_video,
)
from downloader.probe import ProbeResult  # noqa: E402
from jobqueue import batch as batch_module, job_manager as jm  # noqa: E402
from jobqueue.batch import Batch, BatchItem  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import batch_menu  # noqa: E402

USER, CHAT = 42, 7
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabc"


def labels(markup):
    return [b.text for b in markup.buttons()]


def datas(markup):
    return [b.callback_data for b in markup.buttons()]


# ============================================================ pure helpers
class SingleVideoRule(unittest.TestCase):
    def test_links_that_mean_one_video(self):
        for url in ("https://www.youtube.com/watch?v=abc&list=PLx", "https://youtu.be/abc?list=PLx",
                    "https://www.youtube.com/shorts/abc", "https://music.youtube.com/watch?v=a&list=RDx",
                    "https://www.youtube.com/live/abc", "https://www.youtube.com/watch?v=abc&index=3&list=PLx"):
            with self.subTest(url=url):
                self.assertTrue(wants_single_video(url))

    def test_links_that_mean_the_playlist(self):
        for url in (PLAYLIST_URL, "https://www.youtube.com/watch?list=PLx", "https://music.youtube.com/playlist?list=OLAK",
                    "https://soundcloud.com/a/sets/b", "https://vimeo.com/showcase/1", "not a url"):
            with self.subTest(url=url):
                self.assertFalse(wants_single_video(url))

    def test_a_lookalike_domain_is_not_youtube(self):
        self.assertFalse(wants_single_video("https://notyoutube.com/watch?v=abc"))


class PlaylistLinks(unittest.TestCase):
    def test_only_real_playlist_links_skip_the_video_preview(self):
        for url in (PLAYLIST_URL, "https://www.youtube.com/watch?list=PLx", "https://music.youtube.com/playlist?list=OL"):
            self.assertTrue(looks_like_playlist(url), url)
        for url in ("https://www.youtube.com/watch?v=a&list=PLx", "https://youtu.be/a?list=PLx", "https://www.youtube.com/watch?v=a",
                    "https://soundcloud.com/a/sets/b", "https://notyoutube.com/playlist?list=x"):
            self.assertFalse(looks_like_playlist(url), url)


class EntryParsing(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(entry_url({"url": "https://www.youtube.com/watch?v=a"}), "https://www.youtube.com/watch?v=a")
        self.assertEqual(entry_url({"webpage_url": "https://x.com/p", "url": "https://x.com/q"}), "https://x.com/p")
        self.assertEqual(entry_url({"url": "a", "id": "a", "ie_key": "Youtube"}), "https://www.youtube.com/watch?v=a")
        self.assertIsNone(entry_url({"url": "relative-thing"}))
        self.assertIsNone(entry_url({}))

    def test_private_deleted_and_broken_entries_are_dropped(self):
        info = {"entries": [
            {"url": "https://y/1", "title": "Good one", "duration": 61.7},
            {"url": "https://y/2", "title": "[Private video]"},
            {"url": "https://y/3", "title": "[Deleted video]"},
            None,
            {"title": "no url"},
            {"url": "https://y/4", "title": ""},
        ]}
        got = entries_from_info(info)
        self.assertEqual([e.url for e in got], ["https://y/1", "https://y/4"])
        self.assertEqual(got[0].duration, 61)
        self.assertEqual(got[1].title, short_label("https://y/4"))               # untitled -> a readable label

    def test_the_limit(self):
        info = {"entries": [{"url": f"https://y/{i}", "title": f"t{i}"} for i in range(50)]}
        self.assertEqual(len(entries_from_info(info, limit=10)), 10)

    def test_labels(self):
        self.assertEqual(short_label("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "youtube.com/dQw4w9WgXcQ")
        self.assertEqual(short_label("https://youtu.be/abc123"), "youtu.be/abc123")
        self.assertEqual(short_label("https://example.com"), "example.com")


class UrlSplitting(unittest.TestCase):
    def test_links_are_found_deduplicated_and_cleaned(self):
        text = "see https://youtu.be/a, then (https://youtu.be/b) and https://youtu.be/a again!\nhttps://youtu.be/c."
        self.assertEqual(main._unique_urls(text), ["https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/c"])
        self.assertEqual(main._unique_urls("just one https://youtu.be/a"), ["https://youtu.be/a"])
        self.assertEqual(main._unique_urls("no links"), [])


# ============================================================ listing with yt-dlp (stubbed)
class Listing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        yt_dlp.reset([])
        for name, value in (("get_settings", lambda uid: {}), ("cookie_file_for", lambda uid, s: None)):
            self.addCleanup(setattr, playlist_module, name, getattr(playlist_module, name))
            setattr(playlist_module, name, value)

    async def test_lists_a_playlist_flat(self):
        yt_dlp.EXTRACT["result"] = {"title": "My mix", "entries": [
            {"url": "https://y/1", "title": "One", "duration": 100}, {"url": "https://y/2", "title": "Two"}]}
        title, entries = await list_playlist(PLAYLIST_URL, USER)
        self.assertEqual((title, [e.title for e in entries]), ("My mix", ["One", "Two"]))
        opts = yt_dlp.EXTRACT_CALLS[-1][1]
        self.assertEqual(opts["extract_flat"], "in_playlist")                       # no per-video extraction
        self.assertEqual(opts["playlistend"], playlist_module.MAX_LISTED)

    async def test_failures_become_readable_errors(self):
        yt_dlp.EXTRACT["result"] = RuntimeError("HTTP 404")
        yt_dlp.EXTRACT["by_url"][PLAYLIST_URL] = RuntimeError("HTTP 404")
        with self.assertRaisesRegex(ListingError, "Couldn't read this playlist"):
            await list_playlist(PLAYLIST_URL, USER)
        yt_dlp.EXTRACT["by_url"][PLAYLIST_URL] = {"title": "Empty", "entries": [{"url": "https://y/1", "title": "[Private video]"}]}
        with self.assertRaisesRegex(ListingError, "nothing downloadable"):
            await list_playlist(PLAYLIST_URL, USER)

    async def test_a_slow_playlist_times_out_cleanly(self):
        yt_dlp.EXTRACT["delay"] = 0.5
        yt_dlp.EXTRACT["result"] = {"title": "T", "entries": [{"url": "https://y/1", "title": "x"}]}
        self.addCleanup(setattr, playlist_module, "LIST_TIMEOUT", playlist_module.LIST_TIMEOUT)
        playlist_module.LIST_TIMEOUT = 0.05
        with self.assertRaisesRegex(ListingError, "too long"):
            await list_playlist(PLAYLIST_URL, USER)

    async def test_titles_for_pasted_links_with_a_label_for_any_that_fail(self):
        yt_dlp.EXTRACT["by_url"] = {
            "https://youtu.be/a": {"title": "Alpha", "duration": 90},
            "https://youtu.be/b": RuntimeError("private"),
        }
        got = await quick_titles(["https://youtu.be/a", "https://youtu.be/b"], USER)
        self.assertEqual([(e.title, e.duration) for e in got], [("Alpha", 90), ("youtu.be/b", None)])   # order kept
        self.assertTrue(yt_dlp.EXTRACT_CALLS[-1][1]["noplaylist"])

    async def test_one_slow_link_never_holds_up_the_list(self):
        yt_dlp.EXTRACT["delay"] = 0.4
        yt_dlp.EXTRACT["result"] = {"title": "Slow", "duration": 5}
        self.addCleanup(setattr, playlist_module, "TITLES_TIMEOUT", playlist_module.TITLES_TIMEOUT)
        playlist_module.TITLES_TIMEOUT = 0.05
        started = time.monotonic()
        got = await quick_titles(["https://youtu.be/a", "https://youtu.be/b"], USER)
        self.assertLess(time.monotonic() - started, 0.35)
        self.assertEqual([e.title for e in got], ["youtu.be/a", "youtu.be/b"])

    async def test_the_preview_of_a_watch_link_ignores_its_playlist(self):
        yt_dlp.EXTRACT["result"] = {"title": "T", "duration": 60, "formats": []}
        for name, value in (("get_settings", lambda uid: {}), ("cookie_file_for", lambda uid, s: None)):
            self.addCleanup(setattr, probe_module, name, getattr(probe_module, name))
            setattr(probe_module, name, value)
        await probe_module.probe("https://www.youtube.com/watch?v=abc&list=PLx", USER)
        self.assertTrue(yt_dlp.EXTRACT_CALLS[-1][1]["noplaylist"])
        await probe_module.probe(PLAYLIST_URL, USER)
        self.assertFalse(yt_dlp.EXTRACT_CALLS[-1][1]["noplaylist"])
        self.assertEqual(yt_dlp.EXTRACT_CALLS[-1][1]["playlistend"], 1)           # never walk a whole playlist just to label it


# ============================================================ the picker screens
def make_batch(n=20, **kw):
    items = [BatchItem(f"https://y/{i}", f"Video number {i}", 60 + i) for i in range(n)]
    return Batch(bid="bid0000001", user_id=USER, chat_id=CHAT, status_message_id=55, items=items, title="My list", **kw)


class PickerScreens(unittest.TestCase):
    def test_a_page_lists_eight_items_with_marks_and_lengths(self):
        batch = make_batch()
        batch.selected = {1}
        menu = batch_menu.picker_menu(batch, 0)
        self.assertEqual(labels(menu)[0], "○ 1. Video number 0 · 1:00")
        self.assertEqual(labels(menu)[1], "● 2. Video number 1 · 1:01")
        self.assertEqual(len([l for l in labels(menu) if "Video number" in l]), 8)
        for control in ("Select all", "Clear", "✕ Close"):
            self.assertIn(control, labels(menu))
        self.assertIn("↓ Download selected (1)", labels(menu))
        self.assertIn("↓ Download all (20)", labels(menu))

    def test_navigation_only_offers_pages_that_exist(self):
        batch = make_batch(20)
        self.assertEqual(batch_menu.page_count(batch), 3)
        self.assertIn("Next ▶", labels(batch_menu.picker_menu(batch, 0)))
        self.assertNotIn("◀ Prev", labels(batch_menu.picker_menu(batch, 0)))
        middle = labels(batch_menu.picker_menu(batch, 1))
        self.assertIn("◀ Prev", middle)
        self.assertIn("Next ▶", middle)
        last = batch_menu.picker_menu(batch, 2)
        self.assertEqual(len([l for l in labels(last) if "Video number" in l]), 4)
        self.assertNotIn("Next ▶", labels(last))
        self.assertEqual(batch_menu.clamp_page(batch, 99), 2)

    def test_a_short_list_has_no_navigation(self):
        menu = batch_menu.picker_menu(make_batch(3), 0)
        self.assertFalse([l for l in labels(menu) if "Prev" in l or "Next" in l])

    def test_text_and_escaping(self):
        batch = make_batch(3)
        batch.title = "<b>Evil</b> & co"
        text = batch_menu.picker_text(batch, 0, "careful <i>")
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; &amp; co", text)
        self.assertIn("&lt;i&gt;", text)
        self.assertIn("3 items · 0 selected", text)
        self.assertIn("page 2 of 3", batch_menu.picker_text(make_batch(20), 1))

    def test_every_callback_fits_the_64_byte_limit(self):
        batch = make_batch(200)
        batch.bid = "b" * 10
        for menu in (batch_menu.picker_menu(batch, 24), batch_menu.quality_menu(batch), batch_menu.running_menu(batch),
                     batch_menu.summary_menu(batch, 5)):
            for data in datas(menu):
                self.assertLessEqual(len(data.encode()), 64, data)

    def test_quality_screen_offers_every_quality_and_a_way_back(self):
        menu = batch_menu.quality_menu(make_batch())
        got = {d.split("|")[2] for d in datas(menu) if d.startswith("bt|go|")}
        self.assertEqual(got, set(batch_menu.QUALITIES))
        self.assertIn("← Back", labels(menu))

    def test_summary_offers_retry_only_when_something_failed(self):
        self.assertIn("↻ Retry failed (2)", labels(batch_menu.summary_menu(make_batch(), 2)))
        self.assertNotIn("Retry", " ".join(labels(batch_menu.summary_menu(make_batch(), 0))))


# ============================================================ fakes for the end-to-end tests
class FakeSent:
    def __init__(self, bot, message_id):
        self.bot, self.message_id = bot, message_id
        self.chat_id = CHAT

    async def edit_text(self, text, **kw):
        self.bot.edits.append((self.message_id, text, kw.get("reply_markup")))

    async def delete(self):
        self.bot.deleted.append(self.message_id)


class FakeBot:
    def __init__(self):
        self.next_id = 100
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []
        self.deleted: list[int] = []
        self.media: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.next_id += 1
        self.sent.append((self.next_id, text, kw.get("reply_markup")))
        return FakeSent(self, self.next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append((message_id, text, kw.get("reply_markup")))

    edit_message_caption = edit_message_text

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def _media(self, kind, file, kw):
        self.media.append({"kind": kind, "name": getattr(file, "filename", ""), "markup": kw.get("reply_markup")})

    async def send_video(self, chat_id, file, **kw):
        await self._media("video", file, kw)

    async def send_audio(self, chat_id, file, **kw):
        await self._media("audio", file, kw)

    async def send_document(self, chat_id, file, **kw):
        await self._media("document", file, kw)

    def shared_texts(self, message_id):
        return [text for mid, text, _ in self.edits if mid == message_id]


class FakeMessage:
    def __init__(self, text="", message_id=1):
        self.text, self.chat_id, self.message_id = text, CHAT, message_id
        self.photo = []
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, bot, message_id):
        self.data, self.message = data, FakeMessage(message_id=message_id)
        self.bot = bot

        async def delete():
            bot.deleted.append(message_id)
        self.message.delete = delete

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kw):
        self.bot.edits.append((self.message.message_id, text, kw.get("reply_markup")))


class FakeUpdate:
    def __init__(self, bot, query=None, text=None):
        self.callback_query = query
        self.message = FakeMessage(text) if text is not None else None
        self.effective_user = type("U", (), {"id": USER})()
        self.effective_chat = type("C", (), {"id": CHAT})()
        self._bot = bot

    def get_bot(self):
        return self._bot


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


class BatchFlowBase(unittest.IsolatedAsyncioTestCase):
    """A real JobManager (two workers) whose downloader is a fake that produces files, or fails, or hangs."""

    async def asyncSetUp(self):
        yt_dlp.reset([])
        self.bot = FakeBot()
        self.tmp = Path(tempfile.mkdtemp(prefix="batchtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.manager = jm.JobManager(self.bot, max_concurrent=2)
        self.manager.start()
        self.addAsyncCleanup(self._stop_workers)
        self.downloads: list[str] = []

        async def fake_download(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            self.downloads.append(url)
            progress_cb("ytdlp", 50.0, None, None, None, "Video", None)
            await asyncio.sleep(0.03)                      # a real download takes a while; let the update be shown
            if "fail" in url:
                raise RuntimeError("boom: this video is unavailable")
            if "hang" in url:
                while cancel_event is None or not cancel_event.is_set():
                    await asyncio.sleep(0.01)
                from downloader.errors import JobCancelled
                raise JobCancelled("Cancelled by user")
            path = workspace / f"{url.rsplit('/', 1)[-1]}.mp4"
            path.write_bytes(b"x" * 100)
            return [path]

        self._patches = [
            (jm, "dispatch_download", fake_download),
            (jm, "job_workspace", lambda: contextlib.nullcontext(Path(tempfile.mkdtemp(dir=self.tmp)))),
            (jm.ac, "log_download", lambda *a, **k: None),
            (batch_module, "BATCH_MIN_EDIT_INTERVAL", 0.0),
            (main, "job_manager", self.manager),
            (main, "get_settings", lambda uid: dict(DEFAULTS)),
            (main, "pending_batches", {}),
            (main, "pending_links", {}),
        ]
        for module, name, value in self._patches:
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)

        async def allow(*a, **k):
            return True
        for name in ("gate", "gate_callback"):
            self.addCleanup(setattr, main, name, getattr(main, name))
            setattr(main, name, allow)

        async def quiet_delete(message):
            await message.delete()
        self.addCleanup(setattr, main, "_delete_quietly", main._delete_quietly)
        main._delete_quietly = quiet_delete

    async def _stop_workers(self):
        """The worker loop treats a cancellation that lands mid-job as "that job was cancelled" and carries
        on (that is how per-job Cancel works), so one cancel() is not enough for a test that failed mid-run."""
        for _ in range(100):
            for worker in self.manager._workers:
                worker.cancel()
            await asyncio.sleep(0)
            if all(worker.done() for worker in self.manager._workers):
                return

    # -- helpers
    async def tap(self, data, message_id=None):
        bid_message = message_id or self.batch.status_message_id
        await main.batch_callback(FakeUpdate(self.bot, FakeQuery(data, self.bot, bid_message)), FakeContext(self.bot))

    async def send_text(self, text):
        await main.link_handler(FakeUpdate(self.bot, text=text), FakeContext(self.bot))

    @property
    def batch(self) -> Batch:
        return next(iter(main.pending_batches.values()))

    def make_and_register(self, urls, **kw) -> Batch:
        batch = Batch(bid="bid0000009", user_id=USER, chat_id=CHAT, status_message_id=500,
                      items=[BatchItem(u, u.rsplit("/", 1)[-1], 60) for u in urls], title="Mix", **kw)
        main.pending_batches[batch.bid] = batch
        main._touch_rid(batch.bid)
        return batch

    async def until(self, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > end:
                self.fail("timed out waiting")
            await asyncio.sleep(0.01)


# ============================================================ opening the picker
class OpeningThePicker(BatchFlowBase):
    async def test_several_links_open_a_picker_with_titles_in_the_checking_message(self):
        yt_dlp.EXTRACT["by_url"] = {"https://youtu.be/a": {"title": "Alpha", "duration": 90},
                                    "https://youtu.be/b": {"title": "Beta", "duration": 120}}
        await self.send_text("get https://youtu.be/a, https://youtu.be/b and https://youtu.be/a")
        self.assertEqual(self.bot.sent[0][1], "Checking 2 links…")                  # duplicates removed
        batch = self.batch
        self.assertEqual([i.title for i in batch.items], ["Alpha", "Beta"])
        self.assertEqual(batch.status_message_id, self.bot.sent[0][0])             # the checking message became the picker
        text = self.bot.shared_texts(batch.status_message_id)[-1]
        self.assertIn("2 items · 0 selected", text)
        self.assertEqual(len(self.bot.sent), 1)                                    # one message, not one per link

    async def test_more_links_than_the_limit_are_trimmed_with_a_notice(self):
        urls = " ".join(f"https://youtu.be/v{i}" for i in range(batch_menu.MAX_BATCH_DOWNLOAD + 5))
        await self.send_text(urls)
        self.assertEqual(len(self.batch.items), batch_menu.MAX_BATCH_DOWNLOAD)
        self.assertIn(f"first {batch_menu.MAX_BATCH_DOWNLOAD} links", self.bot.shared_texts(self.batch.status_message_id)[-1])

    async def test_non_yt_dlp_sites_get_a_plain_label_without_a_lookup(self):
        await self.send_text("https://open.spotify.com/track/abc https://youtu.be/a")
        spotify = next(i for i in self.batch.items if "spotify" in i.url)
        self.assertEqual(spotify.title, short_label(spotify.url))
        self.assertNotIn("https://open.spotify.com/track/abc", [u for u, _ in yt_dlp.EXTRACT_CALLS])

    async def test_a_youtube_playlist_link_goes_straight_to_the_list_without_a_preview(self):
        async def must_not_probe(url, user_id=None):
            raise AssertionError("a playlist link must not be previewed as a video")
        self.addCleanup(setattr, main, "probe", main.probe)
        main.probe = must_not_probe
        yt_dlp.EXTRACT["by_url"][PLAYLIST_URL] = {"title": "Road trip", "entries": [
            {"url": "https://youtu.be/a", "title": "One", "duration": 61}, {"url": "https://youtu.be/b", "title": "Two"}]}
        await self.send_text(PLAYLIST_URL)
        batch = self.batch
        self.assertEqual((batch.kind, batch.title, [i.title for i in batch.items]), ("playlist", "Road trip", ["One", "Two"]))
        self.assertEqual(len(main.pending_links), 0)                              # no single-link state left behind

    async def test_another_sites_playlist_is_still_found_by_the_preview(self):
        async def fake_probe(url, user_id=None):
            return ProbeResult(ok=True, is_playlist=True, title="PL")
        self.addCleanup(setattr, main, "probe", main.probe)
        main.probe = fake_probe
        url = "https://soundcloud.com/artist/sets/album"
        yt_dlp.EXTRACT["by_url"][url] = {"title": "Album", "entries": [{"url": "https://soundcloud.com/artist/one", "title": "One"}]}
        await self.send_text(url)
        self.assertEqual((self.batch.kind, self.batch.title), ("playlist", "Album"))

    async def test_an_unreadable_playlist_says_so(self):
        yt_dlp.EXTRACT["by_url"][PLAYLIST_URL] = RuntimeError("404")
        await self.send_text(PLAYLIST_URL)
        self.assertEqual(main.pending_batches, {})
        self.assertIn("Couldn't read this playlist", self.bot.edits[-1][1])

    async def test_a_single_link_does_not_open_a_picker(self):
        async def fake_probe(url, user_id=None):
            return ProbeResult(ok=True, title="One", heights=[720], has_audio=True, duration=60)
        self.addCleanup(setattr, main, "probe", main.probe)
        main.probe = fake_probe
        main.pending_probes.clear()
        await self.send_text("https://www.youtube.com/watch?v=abc&list=PLmix")
        self.assertEqual(main.pending_batches, {})                                # a video inside a playlist stays a single video


# ============================================================ sites where gallery-dl is tried first
class PreviewWherever(BatchFlowBase):
    """X and Pinterest try gallery-dl first (right for images), but a video post deserves the full menu too."""

    def set_probe(self, result):
        calls = []

        async def fake(url, user_id=None):
            calls.append(url)
            return result
        self.addCleanup(setattr, main, "probe", main.probe)
        main.probe = fake
        return calls

    def last_markup(self):
        return next(m for _, _, m in reversed(self.bot.edits) if m is not None)

    async def test_an_x_post_with_a_video_gets_the_full_quality_menu(self):
        calls = self.set_probe(ProbeResult(ok=True, title="Clip", heights=[720], has_audio=True, duration=30,
                                           sizes={"best": 5_000_000}))
        await self.send_text("https://x.com/someone/status/123")
        self.assertEqual(calls, ["https://x.com/someone/status/123"])
        texts = labels(self.last_markup())
        self.assertIn("★ Best available ~5.0MB", texts)
        self.assertIn("More options…", texts)

    async def test_an_x_image_post_still_gets_the_gallery_menu(self):
        self.set_probe(ProbeResult(ok=False, error="No video could be found in this tweet"))

        async def gallery_info(url):
            return {"title": "A picture", "thumbnail": ""}
        from downloader import gallerydl_probe
        self.addCleanup(setattr, gallerydl_probe, "probe", gallerydl_probe.probe)
        gallerydl_probe.probe = gallery_info
        await self.send_text("https://x.com/someone/status/456")
        data = datas(self.last_markup())
        self.assertFalse([d for d in data if d.startswith("dl|video|")])
        self.assertTrue([d for d in data if d.startswith("dl|simple")])

    async def test_a_failed_instagram_preview_says_why_in_the_sites_own_words(self):
        calls = self.set_probe(ProbeResult(
            ok=False, error="ERROR: [instagram] DABC123: Requested content is not available, rate-limit reached or login required"))
        await self.send_text("https://www.instagram.com/reel/DABC123/")
        self.assertEqual(calls, ["https://www.instagram.com/reel/DABC123/"])               # yt-dlp IS what looks first
        text = next(t for _, t, m in reversed(self.bot.edits) if m is not None)
        self.assertIn("needs a login", text)
        self.assertIn("rate-limit reached or login required", text)
        self.assertNotIn("DABC123:", text)                                                   # no "[instagram] id:" noise
        self.assertTrue([d for d in datas(self.last_markup()) if d.startswith("dl|")])       # and it can still try downloading

    async def test_when_gallery_dl_finds_a_title_the_person_is_still_told_yt_dlp_failed(self):
        self.set_probe(ProbeResult(ok=False, error="ERROR: [instagram] X: rate-limit reached or login required"))

        async def gallery_info(url):
            return {"title": "A reel", "thumbnail": ""}
        from downloader import gallerydl_probe
        self.addCleanup(setattr, gallerydl_probe, "probe", gallerydl_probe.probe)
        gallerydl_probe.probe = gallery_info
        await self.send_text("https://www.instagram.com/reel/ABC/")
        text = next(t for _, t, m in reversed(self.bot.edits) if m is not None)
        self.assertIn("A reel", text)
        self.assertIn("needs a login", text)
        self.assertIn("rate-limit reached or login required", text)

    async def test_an_x_image_post_found_by_gallery_dl_gets_no_failure_note(self):
        self.set_probe(ProbeResult(ok=False, error="No video could be found in this tweet"))

        async def gallery_info(url):
            return {"title": "A picture", "thumbnail": ""}
        from downloader import gallerydl_probe
        self.addCleanup(setattr, gallerydl_probe, "probe", gallerydl_probe.probe)
        gallerydl_probe.probe = gallery_info
        await self.send_text("https://x.com/someone/status/789")
        text = next(t for _, t, m in reversed(self.bot.edits) if m is not None)
        self.assertIn("A picture", text)
        self.assertNotIn("preview", text)                          # an image post is not a failure

    async def test_a_gallery_only_site_is_never_previewed_with_yt_dlp(self):
        calls = self.set_probe(ProbeResult(ok=True, heights=[720], has_audio=True, duration=30))

        async def gallery_info(url):
            return {"title": "Art", "thumbnail": ""}
        from downloader import gallerydl_probe
        self.addCleanup(setattr, gallerydl_probe, "probe", gallerydl_probe.probe)
        gallerydl_probe.probe = gallery_info
        await self.send_text("https://www.pixiv.net/en/artworks/1")
        self.assertEqual(calls, [])


class PreferYtDlp(unittest.IsolatedAsyncioTestCase):
    async def order_for(self, url, **settings):
        order = []

        def handler(name):
            async def run(*a, **k):
                order.append(name)
                raise Exception(f"{name} failed")
            return run
        original = dict(dispatcher.HANDLERS)
        dispatcher.HANDLERS.update({name: handler(name) for name in original})
        self.addCleanup(dispatcher.HANDLERS.update, original)
        with self.assertRaises(dispatcher.NoToolSucceeded):
            await dispatcher.download(url, Path("/tmp"), settings, 1, lambda *a, **k: None)
        return order

    async def test_x_and_pinterest_try_yt_dlp_first_so_video_keeps_its_full_menu(self):
        for url in ("https://x.com/u/status/1", "https://twitter.com/u/status/1", "https://www.pinterest.com/pin/1/",
                    "https://pin.it/abc"):
            with self.subTest(url=url):
                self.assertEqual((await self.order_for(url))[:2], ["ytdlp", "gallerydl"])

    async def test_an_image_post_chosen_with_the_plain_button_tries_gallery_dl_first(self):
        self.assertEqual((await self.order_for("https://x.com/u/status/1", prefer_gallerydl=True))[:2], ["gallerydl", "ytdlp"])

    async def test_the_gallery_preference_cannot_invent_a_tool_a_site_does_not_have(self):
        self.assertEqual(await self.order_for("https://vimeo.com/1", prefer_gallerydl=True), ["ytdlp"])

    async def test_the_preference_cannot_invent_a_tool_a_site_does_not_have(self):
        self.assertEqual(await self.order_for("https://www.pixiv.net/en/artworks/1", prefer_ytdlp=True), ["gallerydl"])


# ============================================================ picking
class Picking(BatchFlowBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.make_and_register([f"https://y/v{i}" for i in range(20)])

    def text(self):
        return self.bot.shared_texts(500)[-1]

    async def test_tapping_toggles_and_keeps_the_page(self):
        await self.tap("bt|t|3|0|bid0000009")
        self.assertEqual(self.batch.selected, {3})
        self.assertIn("1 selected", self.text())
        await self.tap("bt|t|3|0|bid0000009")
        self.assertEqual(self.batch.selected, set())

    async def test_paging_and_clear_and_select_all(self):
        await self.tap("bt|p|1|bid0000009")
        self.assertIn("page 2 of 3", self.text())
        await self.tap("bt|sa|1|bid0000009")
        self.assertEqual(len(self.batch.selected), 20)
        self.assertIn("page 2 of 3", self.text())
        await self.tap("bt|cl|1|bid0000009")
        self.assertEqual(self.batch.selected, set())

    async def test_the_selection_limit(self):
        self.make_and_register([f"https://y/v{i}" for i in range(80)])
        await self.tap("bt|sa|0|bid0000009")
        self.assertEqual(len(self.batch.selected), batch_menu.MAX_BATCH_DOWNLOAD)
        self.assertIn("first 50", self.text())
        await self.tap("bt|t|60|0|bid0000009")                                    # a 51st
        self.assertIn("up to 50", self.text())
        self.assertNotIn(60, self.batch.selected)

    async def test_download_selected_with_nothing_selected_says_so(self):
        await self.tap("bt|q|bid0000009")
        self.assertIn("Select at least one item", self.text())

    async def test_download_all_selects_everything_then_asks_for_a_quality(self):
        await self.tap("bt|qa|bid0000009")
        self.assertEqual(len(self.batch.selected), 20)
        self.assertIn("Download 20 items", self.text())
        await self.tap("bt|bk|bid0000009")
        self.assertIn("20 selected", self.text())                                 # back to the list, selection kept

    async def test_close_removes_the_picker(self):
        await self.tap("bt|x|bid0000009")
        self.assertEqual(main.pending_batches, {})
        self.assertIn(500, self.bot.deleted)

    async def test_an_unknown_or_someone_elses_list_is_refused(self):
        await self.tap("bt|t|0|0|nope000000")
        self.assertIn("expired", self.bot.edits[-1][1])
        self.batch.user_id = 999
        await self.tap("bt|t|0|0|bid0000009")
        self.assertEqual(self.batch.selected, set())

    async def test_a_forged_quality_does_nothing(self):
        self.batch.selected = {0}
        await self.tap("bt|go|ultra4k|bid0000009")
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.batch.run_indices, [])

    async def test_state_is_swept_unless_the_batch_is_still_running(self):
        batch = self.batch
        main._rid_last_touch[batch.bid] = time.time() - main.RID_STATE_TTL_SECONDS - 5
        batch.run_indices, batch.finished = [0], False                             # started and still going
        main._sweep_stale_state_once()
        self.assertIn(batch.bid, main.pending_batches)
        batch.finished = True
        main._rid_last_touch[batch.bid] = time.time() - main.RID_STATE_TTL_SECONDS - 5
        main._sweep_stale_state_once()
        self.assertNotIn(batch.bid, main.pending_batches)


# ============================================================ running a batch
class Running(BatchFlowBase):
    def start(self, urls, quality="720p", **kw):
        self.make_and_register(urls, **kw)
        self.batch.selected = set(range(len(urls)))

    async def go(self, quality="720p"):
        await self.tap(f"bt|go|{quality}|bid0000009")

    async def finished(self):
        await self.until(lambda: self.batch.finished)

    async def test_everything_delivered_leaves_no_message_behind(self):
        self.start([f"https://y/v{i}" for i in range(4)])
        await self.go("720p")
        await self.finished()
        await self.until(lambda: 500 in self.bot.deleted)
        self.assertEqual(sorted(m["name"] for m in self.bot.media), ["v0.mp4", "v1.mp4", "v2.mp4", "v3.mp4"])
        self.assertEqual(sorted(self.downloads), [f"https://y/v{i}" for i in range(4)])

    async def test_each_item_uses_the_chosen_quality(self):
        seen = []
        original = jm.dispatch_download

        async def spy(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            seen.append((url, settings["mode"], settings.get("quality"), settings.get("audio_format")))
            return await original(url, workspace, settings, user_id, progress_cb, cancel_event)
        jm.dispatch_download = spy
        self.start(["https://y/a", "https://y/b"])
        await self.go("mp3")
        await self.finished()
        self.assertTrue(all(mode == "audio" and fmt == "mp3" for _, mode, _, fmt in seen), seen)

    async def test_a_spotify_item_is_always_audio(self):
        modes = {}
        original = jm.dispatch_download

        async def spy(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            modes[url] = settings["mode"]
            return await original(url, workspace, settings, user_id, progress_cb, cancel_event)
        jm.dispatch_download = spy
        self.start(["https://y/a", "https://open.spotify.com/track/abc"])
        await self.go("best")
        await self.finished()
        self.assertEqual(modes, {"https://y/a": "video", "https://open.spotify.com/track/abc": "audio"})

    async def test_only_the_shared_message_is_edited_and_it_shows_progress(self):
        self.start([f"https://y/v{i}" for i in range(3)])
        await self.go()
        await self.finished()
        edited_ids = {mid for mid, _, _ in self.bot.edits}
        self.assertEqual(edited_ids, {500})                                        # no per-item status messages
        running = [t for t in self.bot.shared_texts(500) if "Downloading" in t]
        self.assertTrue(running)
        self.assertTrue(any("of 3 done" in t for t in running))
        self.assertTrue(any("[✦]" in t and "50%" in t for t in running))            # a running item with its percent
        self.assertTrue(all("↳ 🎬 Video | 📋 3 items" in t for t in self.bot.shared_texts(500)))

    async def test_no_dead_end_send_as_file_button_on_batch_items(self):
        self.start(["https://y/a", "https://y/b"])
        await self.go()
        await self.finished()
        for media in self.bot.media:
            self.assertNotIn("▤ Send as file instead", labels(media["markup"]))
            self.assertNotIn("▤ Send all as files", labels(media["markup"]))
        self.assertEqual(self.manager._recent_files, {})                           # nothing cached for batch items

    async def test_a_failure_keeps_a_summary_with_the_reason_and_a_retry(self):
        self.start(["https://y/ok1", "https://y/fail1", "https://y/ok2"])
        await self.go()
        await self.finished()
        text = self.bot.shared_texts(500)[-1]
        self.assertIn("Finished · 2 of 3 delivered", text)
        self.assertIn("[✕] 1 failed", text)
        self.assertIn("2. fail1 — boom: this video is unavailable", text)
        self.assertNotIn(500, self.bot.deleted)
        final_markup = next(m for mid, t, m in reversed(self.bot.edits) if mid == 500)
        self.assertIn("↻ Retry failed (1)", labels(final_markup))

    async def test_retry_runs_only_the_failed_items_and_then_cleans_up(self):
        self.start(["https://y/ok1", "https://y/fail1"])
        await self.go()
        await self.finished()
        self.downloads.clear()
        # the "video" is available now
        original = jm.dispatch_download

        async def healthy(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            return await original(url.replace("fail", "fixed"), workspace, settings, user_id, progress_cb, cancel_event)
        jm.dispatch_download = healthy
        await self.tap("bt|rt|bid0000009")
        await self.until(lambda: 500 in self.bot.deleted)
        self.assertEqual(self.downloads, ["https://y/fixed1"])                      # ok1 was not downloaded again

    async def test_cancel_all_stops_running_and_waiting_items(self):
        self.start([f"https://y/hang{i}" for i in range(5)])                         # 2 run, 3 wait
        await self.go()
        await self.until(lambda: sum(1 for i in self.batch.items if i.state == "running") == 2)
        await self.tap("bt|cx|bid0000009")
        await self.finished()
        self.assertTrue(all(i.state == "cancelled" for i in self.batch.items))
        text = self.bot.shared_texts(500)[-1]
        self.assertIn("Cancelled · 0 of 5 delivered", text)
        self.assertIn("[✕] 5 cancelled", text)
        self.assertEqual(self.bot.media, [])

    async def test_dismissing_the_summary_deletes_it(self):
        self.start(["https://y/fail1"])
        await self.go()
        await self.finished()
        await self.tap("bt|dm|bid0000009")
        self.assertIn(500, self.bot.deleted)
        self.assertEqual(main.pending_batches, {})

    async def test_picker_buttons_are_ignored_once_it_has_started(self):
        self.start(["https://y/hang1"])
        await self.go()
        await self.until(lambda: self.batch.items[0].state == "running")
        before = len(self.bot.edits)
        await self.tap("bt|t|0|0|bid0000009")
        await self.tap("bt|cl|0|bid0000009")
        self.assertEqual(self.batch.selected, {0})
        self.assertEqual(len(self.bot.edits), before)                              # nothing was redrawn
        await self.tap("bt|cx|bid0000009")
        await self.finished()

    async def test_edits_are_throttled_but_the_last_state_always_lands(self):
        batch_module.BATCH_MIN_EDIT_INTERVAL = 0.3
        self.start([f"https://y/v{i}" for i in range(6)])
        await self.go()
        await self.until(lambda: 500 in self.bot.deleted, timeout=8)
        self.assertLess(len(self.bot.edits), 20)
        self.assertEqual(len(self.bot.media), 6)

    async def test_the_history_gets_one_entry_per_item_with_its_title(self):
        logged = []
        jm.ac.log_download = lambda *a, **k: logged.append(a)
        self.start(["https://y/a", "https://y/b"])
        await self.go()
        await self.finished()
        self.assertEqual(sorted(entry[1] for entry in logged), ["https://y/a", "https://y/b"])
        self.assertEqual({entry[2] for entry in logged}, {"success"})


if __name__ == "__main__":
    unittest.main()
