"""Routing YouTube through the WARP proxy only when the server's address is blocked."""
import shutil
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
from downloader import playlist as playlist_module, probe as probe_module, proxy as proxy_module, ytdlp_handler  # noqa: E402
from downloader import dispatcher  # noqa: E402
from downloader.proxy import ProxyPolicy, is_ip_block, is_youtube, site_for, site_label  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402

YT = "https://www.youtube.com/watch?v=abc"
X = "https://x.com/u/status/1"
IG = "https://www.instagram.com/p/abc/"
WARP = "socks5h://warp:1080"
WARP2 = "socks5h://warp2:1080"
BLOCK = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(proxies=(WARP,), mode="auto", alive=lambda p: True):
    clock = Clock()
    return ProxyPolicy(list(proxies), mode, clock=clock, alive=alive, direct_block_seconds=100, proxy_block_seconds=50), clock


class Detection(unittest.TestCase):
    def test_what_counts_as_an_address_block(self):
        for text in (BLOCK, "HTTP Error 403: Forbidden", "HTTP Error 429: Too Many Requests", "Too many requests"):
            self.assertTrue(is_ip_block(text), text)
        for text in ("Video unavailable", "Private video", "ffmpeg failed", "timed out", "", None):
            self.assertFalse(is_ip_block(text), text)

    def test_only_youtube_is_routed(self):
        for url in ("https://youtu.be/a", "https://m.youtube.com/watch?v=a", "https://music.youtube.com/x"):
            self.assertTrue(is_youtube(url))
        for url in ("https://vimeo.com/1", "https://notyoutube.com/a", "https://x.com/u/status/1"):
            self.assertFalse(is_youtube(url))


class Policy(unittest.TestCase):
    def test_off_or_no_proxy_means_always_direct(self):
        self.assertIsNone(make(mode="off")[0].route(YT))
        self.assertIsNone(make(proxies=())[0].route(YT))
        policy, _ = make(mode="off")
        self.assertFalse(policy.report_failure(YT, None, BLOCK))

    def test_other_sites_never_use_the_proxy(self):
        policy, _ = make(mode="always")
        self.assertIsNone(policy.route("https://vimeo.com/1"))
        self.assertFalse(policy.report_failure("https://vimeo.com/1", None, BLOCK))

    def test_auto_goes_direct_until_youtube_blocks_then_uses_the_proxy_for_a_while(self):
        policy, clock = make()
        self.assertIsNone(policy.route(YT))
        self.assertTrue(policy.report_failure(YT, None, BLOCK))                 # worth retrying: the route changed
        self.assertEqual(policy.route(YT), WARP)
        clock.now += 99
        self.assertEqual(policy.route(YT), WARP)
        clock.now += 2                                                         # the block window has passed
        self.assertIsNone(policy.route(YT))

    def test_a_direct_success_ends_the_block_window_early(self):
        policy, _ = make()
        policy.report_failure(YT, None, BLOCK)
        policy.report_success(YT, None)
        self.assertIsNone(policy.route(YT))

    def test_errors_that_are_not_about_the_address_change_nothing(self):
        policy, _ = make()
        self.assertFalse(policy.report_failure(YT, None, "Video unavailable"))
        self.assertIsNone(policy.route(YT))

    def test_always_mode_uses_the_proxy_whenever_it_is_up(self):
        policy, _ = make(mode="always")
        self.assertEqual(policy.route(YT), WARP)
        down, _ = make(mode="always", alive=lambda p: False)
        self.assertIsNone(down.route(YT))                                      # the container is down: carry on direct

    def test_a_proxy_that_is_not_running_is_skipped_even_after_a_block(self):
        policy, _ = make(alive=lambda p: False)
        self.assertFalse(policy.report_failure(YT, None, BLOCK))               # nowhere to go: no pointless retry
        self.assertIsNone(policy.route(YT))

    def test_the_liveness_check_is_cached_briefly(self):
        calls = []
        policy, clock = make(mode="always", alive=lambda p: calls.append(p) or True)
        for _ in range(5):
            policy.route(YT)
        self.assertEqual(len(calls), 1)
        clock.now += 16
        policy.route(YT)
        self.assertEqual(len(calls), 2)

    def test_a_blocked_proxy_is_set_aside_and_the_next_one_used(self):
        policy, clock = make(proxies=(WARP, WARP2), mode="always")
        self.assertEqual(policy.route(YT), WARP)
        self.assertTrue(policy.report_failure(YT, WARP, BLOCK))
        self.assertEqual(policy.route(YT), WARP2)
        clock.now += 51
        self.assertEqual(policy.route(YT), WARP)                               # forgiven after a while

    def test_when_every_proxy_is_blocked_it_falls_back_to_direct(self):
        policy, _ = make(mode="always")
        self.assertTrue(policy.report_failure(YT, WARP, BLOCK))                # direct is the only route left
        self.assertIsNone(policy.route(YT))
        self.assertFalse(policy.report_failure(YT, None, BLOCK))               # and that already failed: stop

    def test_the_proxy_that_worked_last_is_preferred(self):
        policy, _ = make(proxies=(WARP, WARP2), mode="always")
        policy.report_success(YT, WARP2)
        self.assertEqual(policy.route(YT), WARP2)

    def test_logs_never_show_proxy_credentials(self):
        self.assertEqual(proxy_module._hide("socks5h://user:secret@warp:1080"), "socks5h://warp:1080")


class OtherSites(unittest.TestCase):
    """The same block-then-WARP logic applies to every site in PROXY_DOMAINS, each tracked on its own."""

    def test_the_default_list_covers_the_sites_that_block_servers(self):
        for url in (YT, X, "https://twitter.com/u/status/1", IG, "https://www.pinterest.com/pin/1/", "https://pin.it/a",
                    "https://www.reddit.com/r/a/", "https://www.tiktok.com/@a/video/1"):
            self.assertIsNotNone(site_for(url), url)
        for url in ("https://vimeo.com/1", "https://dropbox.com/a", "https://notx.com/a", "https://example.com/x.mp4"):
            self.assertIsNone(site_for(url), url)

    def test_hostnames_of_one_site_share_a_record(self):
        self.assertEqual(site_for("https://twitter.com/a"), site_for(X))
        self.assertEqual(site_for("https://youtu.be/a"), site_for(YT))
        self.assertEqual(site_for("https://pin.it/a"), site_for("https://www.pinterest.com/pin/1/"))

    def test_a_block_on_one_site_does_not_send_the_others_through_the_proxy(self):
        policy, _ = make()
        self.assertTrue(policy.report_failure(IG, None, "HTTP Error 429: Too Many Requests"))
        self.assertEqual(policy.route(IG), WARP)
        self.assertIsNone(policy.route(YT))
        self.assertIsNone(policy.route(X))

    def test_a_direct_success_on_one_site_does_not_end_anothers_block_window(self):
        policy, _ = make()
        policy.report_failure(IG, None, BLOCK)
        policy.report_failure(X, None, "HTTP Error 403: Forbidden")
        policy.report_success(IG, None)
        self.assertIsNone(policy.route(IG))
        self.assertEqual(policy.route(X), WARP)

    def test_a_proxy_blocked_by_one_site_is_still_used_for_another(self):
        policy, _ = make(mode="always")
        policy.report_failure(IG, WARP, BLOCK)
        self.assertIsNone(policy.route(IG))                                     # set aside for Instagram only
        self.assertEqual(policy.route(X), WARP)

    def test_the_site_list_is_configurable_and_star_means_everything(self):
        only_x = ProxyPolicy([WARP], "always", alive=lambda p: True, domains=["x.com"])
        self.assertEqual(only_x.route(X), WARP)
        self.assertIsNone(only_x.route(YT))
        everything = ProxyPolicy([WARP], "always", alive=lambda p: True, domains=["*"])
        self.assertEqual(everything.route("https://vimeo.com/1"), WARP)

    def test_gallery_dl_and_instagram_wordings_count_as_blocks(self):
        for text in ("HttpError: '403 Forbidden' for 'https://x.com/i/api'", "HttpError: '429 Too Many Requests'",
                     "ERROR: [instagram] abc: Instagram API is not granting access: rate-limit reached or login required"):
            self.assertTrue(is_ip_block(text), text)
        self.assertFalse(is_ip_block("NotFoundError: post not found"))

    def test_messages_name_the_site(self):
        self.assertEqual((site_label(YT), site_label(X), site_label("https://m.youtube.com/a")), ("YouTube", "X", "YouTube"))
        self.assertEqual(site_label("https://www.vimeo.com/1"), "vimeo.com")


class GalleryDlRouting(unittest.IsolatedAsyncioTestCase):
    """gallery-dl has no routing of its own: the dispatcher gives it the same direct-then-proxy behaviour."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="gdlroute-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.proxies_seen: list = []
        self.stages: list[str] = []
        self.addCleanup(setattr, dispatcher, "policy", dispatcher.policy)
        dispatcher.policy, _ = make()
        original = dict(dispatcher.HANDLERS)
        self.addCleanup(dispatcher.HANDLERS.update, original)

    def install(self, outcomes):
        outcomes = list(outcomes)

        async def gallerydl(url, workspace, settings, user_id, cb, cancel_event=None):
            self.proxies_seen.append(settings.get("proxy") or None)
            (workspace / "leftover.part").write_text("x")
            outcome = outcomes.pop(0)
            if outcome != "ok":
                raise RuntimeError(outcome)
            return [workspace / "leftover.part"]
        dispatcher.HANDLERS["gallerydl"] = gallerydl

        async def ytdlp_fails(*a, **k):
            raise RuntimeError("No video formats found")
        dispatcher.HANDLERS["ytdlp"] = ytdlp_fails

    async def run_download(self, url, **settings):
        def cb(*args):
            if len(args) > 4 and args[4]:
                self.stages.append(args[4])
        return await dispatcher.download(url, self.workspace, {**DEFAULTS, "prefer_gallerydl": True, **settings}, 1, cb)

    async def test_a_blocked_gallery_dl_download_is_retried_through_the_proxy(self):
        self.install(["HttpError: '403 Forbidden'", "ok"])
        files = await self.run_download(X)
        self.assertEqual(len(files), 1)
        self.assertEqual(self.proxies_seen, [None, WARP])
        self.assertTrue(any("Switching route - X blocked this address" in stage for stage in self.stages), self.stages)

    async def test_the_next_one_goes_straight_to_the_proxy(self):
        self.install(["HttpError: '403 Forbidden'", "ok", "ok"])
        await self.run_download(X)
        self.proxies_seen.clear()
        await self.run_download(X)
        self.assertEqual(self.proxies_seen, [WARP])

    async def test_a_site_that_is_not_blocked_stays_direct(self):
        self.install(["ok"])
        await self.run_download(X)
        self.assertEqual(self.proxies_seen, [None])

    async def test_other_errors_are_not_retried(self):
        self.install(["NotFoundError: post not found"])
        with self.assertRaises(dispatcher.NoToolSucceeded):
            await self.run_download(X)
        self.assertEqual(self.proxies_seen, [None])

    async def test_someones_own_proxy_is_left_alone(self):
        self.install(["HttpError: '403 Forbidden'"])
        with self.assertRaises(dispatcher.NoToolSucceeded):
            await self.run_download(X, proxy="http://mine:3128")
        self.assertEqual(self.proxies_seen, ["http://mine:3128"])

    async def test_direct_file_downloads_are_never_routed_because_aria2c_cannot_use_socks(self):
        dispatcher.policy, _ = make(mode="always")
        seen = []

        async def generic(url, workspace, settings, user_id, cb, cancel_event=None):
            seen.append(settings.get("proxy") or None)
            return [workspace / "a.bin"]
        dispatcher.HANDLERS["generic"] = generic

        async def nope(*a, **k):
            raise RuntimeError("no")
        dispatcher.HANDLERS["ytdlp"] = nope
        dispatcher.HANDLERS["gallerydl"] = nope
        await dispatcher.download("https://example.com/a.bin", self.workspace, {**DEFAULTS}, 1, lambda *a, **k: None)
        self.assertEqual(seen, [None])


# ============================================================ integration with the real call sites
class Downloads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="proxy-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.stages: list[str] = []

    def use(self, policy):
        self.addCleanup(setattr, ytdlp_handler, "policy", ytdlp_handler.policy)
        ytdlp_handler.policy = policy

    async def download(self, script, **settings):
        yt_dlp.reset(script)
        full = {**DEFAULTS, "mode": "video", "quality": "best", **settings}

        def cb(*args):
            if len(args) > 3 and args[3]:
                self.stages.append(args[3])
        files = await ytdlp_handler.download(YT, self.workspace, full, 1, cb)
        return files, full

    def blocked_direct_attempt(self):
        """One refused attempt = the first try plus every client fallback."""
        return ["botcheck"] * (1 + len(ytdlp_handler.CLIENT_FALLBACKS))

    async def test_a_blocked_direct_download_is_retried_through_the_proxy(self):
        policy, _ = make()
        self.use(policy)
        files, _ = await self.download(self.blocked_direct_attempt() + ["ok"])
        self.assertEqual(len(files), 1)
        self.assertNotIn("proxy", yt_dlp.CALLS[0])                               # first try: direct
        self.assertEqual(yt_dlp.CALLS[-1]["proxy"], WARP)                        # the retry: via WARP
        self.assertIn("Switching route - YouTube blocked this address", self.stages)
        self.assertEqual(policy.route(YT), WARP)                                 # and it remembers

    async def test_the_next_download_goes_straight_to_the_proxy(self):
        policy, _ = make()
        self.use(policy)
        await self.download(self.blocked_direct_attempt() + ["ok"])
        await self.download(["ok"])
        self.assertEqual(len(yt_dlp.CALLS), 1)
        self.assertEqual(yt_dlp.CALLS[0]["proxy"], WARP)

    async def test_a_download_that_works_direct_never_touches_the_proxy(self):
        policy, _ = make()
        self.use(policy)
        await self.download(["ok"])
        self.assertNotIn("proxy", yt_dlp.CALLS[0])

    async def test_someones_own_proxy_is_respected_and_the_policy_stays_out_of_it(self):
        policy, _ = make(mode="always")
        self.use(policy)
        with self.assertRaises(Exception):
            await self.download(self.blocked_direct_attempt(), proxy="http://mine:3128")
        self.assertTrue(all(call["proxy"] == "http://mine:3128" for call in yt_dlp.CALLS))
        self.assertEqual(len(yt_dlp.CALLS), 1 + len(ytdlp_handler.CLIENT_FALLBACKS))   # no second route tried

    async def test_other_errors_are_not_retried_on_another_route(self):
        policy, _ = make(mode="always")
        self.use(policy)
        with self.assertRaisesRegex(Exception, "boom"):
            await self.download(["fail"])
        self.assertEqual(len(yt_dlp.CALLS), 1)

    async def test_only_one_retry_even_if_the_proxy_is_blocked_too(self):
        policy, _ = make()
        self.use(policy)
        with self.assertRaises(Exception):
            await self.download(self.blocked_direct_attempt() * 2)
        self.assertEqual(len(yt_dlp.CALLS), 2 * (1 + len(ytdlp_handler.CLIENT_FALLBACKS)))

    async def test_notes_from_the_retry_reach_the_job(self):
        policy, _ = make(mode="always")
        self.use(policy)
        files, settings = await self.download(["ppfail", "ok"])                  # cover-art fallback inside the proxied attempt
        self.assertTrue(settings["delivery_notes"])

    async def test_other_sites_are_untouched(self):
        policy, _ = make(mode="always")
        self.use(policy)
        yt_dlp.reset(["ok"])
        await ytdlp_handler.download("https://vimeo.com/1", self.workspace, {**DEFAULTS, "mode": "video", "quality": "best"},
                                     1, lambda *a: None)
        self.assertNotIn("proxy", yt_dlp.CALLS[0])


class Previews(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        yt_dlp.reset([])
        self.seen: list = []
        original = yt_dlp.YoutubeDL.extract_info
        self.addCleanup(setattr, yt_dlp.YoutubeDL, "extract_info", original)

        def extract(inner, url, download=False):
            self.seen.append(inner.opts.get("proxy"))
            if inner.opts.get("proxy") is None and self.block_direct:
                raise Exception(BLOCK)
            return {"title": "T", "duration": 60, "formats": [], "entries": [{"url": "https://y/1", "title": "One"}]}
        yt_dlp.YoutubeDL.extract_info = extract
        self.block_direct = True
        for module in (probe_module, playlist_module):
            for name, value in (("get_settings", lambda uid: {}), ("cookie_file_for", lambda uid, s: None)):
                self.addCleanup(setattr, module, name, getattr(module, name))
                setattr(module, name, value)
        self.policy, _ = make()
        for module in (probe_module, playlist_module):
            self.addCleanup(setattr, module, "policy", module.policy)
            module.policy = self.policy

    async def test_a_blocked_preview_is_retried_through_the_proxy(self):
        result = await probe_module.probe(YT, 1)
        self.assertTrue(result.ok)
        self.assertEqual(self.seen, [None, WARP])

    async def test_a_blocked_playlist_listing_is_retried_through_the_proxy(self):
        title, entries = await playlist_module.list_playlist("https://www.youtube.com/playlist?list=PL1", 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.seen, [None, WARP])

    async def test_once_direct_is_known_blocked_the_next_preview_goes_straight_to_the_proxy(self):
        await probe_module.probe(YT, 1)
        self.seen.clear()
        await probe_module.probe(YT, 1)
        self.assertEqual(self.seen, [WARP])

    async def test_a_preview_that_works_direct_is_left_alone(self):
        self.block_direct = False
        result = await probe_module.probe(YT, 1)
        self.assertTrue(result.ok)
        self.assertEqual(self.seen, [None])


class LowPriority(unittest.TestCase):
    def setUp(self):
        import main, os
        self.main, self.os = main, os
        self.calls = []
        for target, name, value in ((os, "nice", lambda n: self.calls.append(n)),):
            self.addCleanup(setattr, target, name, getattr(target, name))
            setattr(target, name, value)

    def test_the_bot_lowers_its_priority_so_ffmpeg_cannot_starve_the_host(self):
        self.addCleanup(setattr, self.main.config, "PROCESS_NICE", self.main.config.PROCESS_NICE)
        self.main.config.PROCESS_NICE = 10
        self.main._lower_priority()
        self.assertEqual(self.calls, [10])

    def test_zero_means_leave_it_alone_and_failures_are_harmless(self):
        self.addCleanup(setattr, self.main.config, "PROCESS_NICE", self.main.config.PROCESS_NICE)
        self.main.config.PROCESS_NICE = 0
        self.main._lower_priority()
        self.assertEqual(self.calls, [])
        self.main.config.PROCESS_NICE = 5
        self.os.nice = lambda n: (_ for _ in ()).throw(OSError("not permitted"))
        self.main._lower_priority()                                     # must not raise


if __name__ == "__main__":
    unittest.main()
