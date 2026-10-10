"""
"Change WARP address" in the admin panel: the bot-side client (cooldown, one at a time, old -> new address),
the proxy policy forgetting blocks, the admin screens (warning while downloads run, owner/admin only), and the
control service itself driven against a fake Docker.
"""
import asyncio
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import _env  # noqa: F401  (must come first)

import config  # noqa: E402
import main  # noqa: E402
from downloader import proxy as proxy_module  # noqa: E402
from downloader import warp_control as wc  # noqa: E402
from ui import admin_menu  # noqa: E402
from tests.test_sections_flow import FakeBot, FakeContext, FakeQuery, FakeUpdate, datas, labels  # noqa: E402

WARP = "socks5h://warp:1080"
YT = "https://www.youtube.com/watch?v=abc"


class Setup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("WARP_CONTROL_URL", "http://warp-control:8765"), ("WARP_CONTROL_TOKEN", "secret"),
                            ("WARP_ROTATE_COOLDOWN_SECONDS", 120), ("YT_PROXIES", [WARP])):
            self.addCleanup(setattr, config, name, getattr(config, name))
            setattr(config, name, value)
        self.addCleanup(setattr, wc, "_last_change", wc._last_change)
        wc._last_change = 0.0
        self.addCleanup(setattr, proxy_module, "policy", proxy_module.policy)
        proxy_module.policy = proxy_module.ProxyPolicy([WARP], "auto", alive=lambda p: True, domains=["youtube.com"])
        self.ips = ["1.1.1.1", "2.2.2.2"]
        self.posts = []
        self.answer = {"ok": True, "detail": "connected"}

        async def current_ip(proxy=None):
            return self.ips.pop(0) if self.ips else None

        def post():
            self.posts.append(1)
            return self.answer
        for name, value in (("current_ip", current_ip), ("_post_rotate", post)):
            self.addCleanup(setattr, wc, name, getattr(wc, name))
            setattr(wc, name, value)
        self.addCleanup(setattr, wc.asyncio, "sleep", wc.asyncio.sleep)


class Client(Setup):
    async def test_a_change_reports_old_and_new_address_and_clears_blocks(self):
        proxy_module.policy.report_failure(YT, WARP, "Sign in to confirm you're not a bot")
        self.assertIsNone(proxy_module.policy.route(YT))                         # WARP was set aside for YouTube
        result = await wc.rotate()
        self.assertEqual((result.ok, result.old_ip, result.new_ip), (True, "1.1.1.1", "2.2.2.2"))
        self.assertEqual(self.posts, [1])
        proxy_module.policy.report_failure(YT, None, "HTTP Error 429")           # direct is blocked -> use WARP again
        self.assertEqual(proxy_module.policy.route(YT), WARP)                    # the new address is not marked blocked

    async def test_the_same_address_is_reported_honestly(self):
        self.ips = ["1.1.1.1", "1.1.1.1"]
        result = await wc.rotate()
        self.assertTrue(result.ok)
        self.assertIn("same address", result.message)

    async def test_a_failure_from_the_control_service_is_passed_on_and_blocks_are_kept(self):
        self.answer = {"ok": False, "detail": "restart failed (500)"}
        proxy_module.policy = proxy_module.ProxyPolicy([WARP], "always", alive=lambda p: True, domains=["youtube.com"])
        proxy_module.policy.report_failure(YT, WARP, "Sign in to confirm you're not a bot")
        self.assertIsNone(proxy_module.policy.route(YT))
        result = await wc.rotate()
        self.assertFalse(result.ok)
        self.assertIn("restart failed", result.message)
        self.assertIsNone(proxy_module.policy.route(YT))                         # nothing changed, nothing forgotten

    async def test_not_configured_does_nothing(self):
        config.WARP_CONTROL_TOKEN = ""
        result = await wc.rotate()
        self.assertFalse(result.ok)
        self.assertEqual(self.posts, [])

    async def test_a_cooldown_stops_back_to_back_changes_even_after_a_failure(self):
        self.answer = {"ok": False, "detail": "boom"}
        await wc.rotate()
        again = await wc.rotate()
        self.assertIn("wait", again.message)
        self.assertEqual(len(self.posts), 1)
        wc._last_change = time.time() - 121
        self.answer = {"ok": True}
        self.ips = ["1.1.1.1", "2.2.2.2"]
        self.assertTrue((await wc.rotate()).ok)

    async def test_only_one_change_runs_at_a_time(self):
        gate = asyncio.Event()

        def slow_post():
            self.posts.append(1)
            return self.answer
        async def slow_ip(proxy=None):
            await gate.wait()
            return "1.1.1.1"
        wc.current_ip = slow_ip
        first = asyncio.create_task(wc.rotate())
        await asyncio.sleep(0.05)
        self.assertTrue(wc.busy())
        second = await asyncio.wait_for(wc.rotate(), timeout=2)                 # must answer at once, not queue up
        self.assertIn("already running", second.message)
        gate.set()
        await first
        self.assertEqual(len(self.posts), 1)

    async def test_a_restarted_proxy_that_does_not_answer_is_not_called_a_success(self):
        async def never(proxy=None):
            return None
        wc.current_ip = never

        async def fast_sleep(seconds):
            return None
        wc.asyncio.sleep = fast_sleep
        result = await wc.rotate()
        self.assertFalse(result.ok)
        self.assertIn("isn't answering", result.message)

    def test_the_policy_reset_is_limited_to_the_given_proxy(self):
        policy = proxy_module.ProxyPolicy([WARP, "socks5h://other:1080"], "always", alive=lambda p: True, domains=["youtube.com"])
        policy.report_failure(YT, WARP, "HTTP Error 429")
        policy.report_failure(YT, "socks5h://other:1080", "HTTP Error 429")
        self.assertIsNone(policy.route(YT))
        policy.reset_proxy(WARP)
        self.assertEqual(policy.route(YT), WARP)
        self.assertEqual(policy._usable("youtube.com"), [WARP])                  # the other proxy's block is untouched


class Panel(Setup):
    def setUp(self):
        super().setUp()
        self.bot = FakeBot()
        self.running = 0
        self.addCleanup(setattr, main, "is_owner_or_admin", main.is_owner_or_admin)
        main.is_owner_or_admin = lambda uid: uid == 42
        self.addCleanup(setattr, main, "job_manager", main.job_manager)
        main.job_manager = mock.Mock(active_count=lambda: self.running)
        self.addCleanup(setattr, main, "tcp_alive", main.tcp_alive)
        main.tcp_alive = lambda proxy: True
        self.ips = ["1.1.1.1"]
        self.shown = []

    async def tap(self, data, user=None):
        query = FakeQuery(data, self.bot)
        update = FakeUpdate(self.bot, query)
        if user:
            update.effective_user.id = user
        await main.admin_panel_callback(update, FakeContext(self.bot))
        return self.bot.edits[-1]

    def test_the_main_panel_has_a_warp_button(self):
        self.assertIn("🌐 WARP", labels(admin_menu.main_panel()))
        self.assertIn("adm|warp", datas(admin_menu.main_panel()))

    async def test_the_screen_shows_status_address_and_running_downloads(self):
        self.running = 2
        edit = await self.tap("adm|warp")
        self.assertIn("✓ reachable", edit["text"])
        self.assertIn("<code>1.1.1.1</code>", edit["text"])
        self.assertIn("Downloads running: 2", edit["text"])
        self.assertEqual(labels(edit["markup"]), ["🔄 Change address", "↻ Refresh", "← Back"])

    async def test_changing_with_no_downloads_running_goes_straight_ahead(self):
        self.ips = ["1.1.1.1", "2.2.2.2", "2.2.2.2"]
        edit = await self.tap("adm|warp_ip")
        self.assertEqual(self.posts, [1])
        self.assertIn("<code>1.1.1.1</code> → <code>2.2.2.2</code>", edit["text"])

    async def test_changing_while_downloads_run_asks_first_and_nothing_happens_yet(self):
        self.running = 3
        edit = await self.tap("adm|warp_ip")
        self.assertIn("<b>3</b> download(s)", edit["text"])
        self.assertEqual(datas(edit["markup"]), ["adm|warp_go", "adm|warp"])
        self.assertEqual(self.posts, [])

    async def test_confirming_goes_ahead_even_with_downloads_running(self):
        self.running = 3
        self.ips = ["1.1.1.1", "2.2.2.2", "2.2.2.2"]
        await self.tap("adm|warp_go")
        self.assertEqual(self.posts, [1])

    async def test_a_failure_is_shown_on_the_screen(self):
        self.answer = {"ok": False, "detail": "restart failed (500)"}
        self.ips = ["1.1.1.1", "1.1.1.1"]
        edit = await self.tap("adm|warp_ip")
        self.assertIn("✕ WARP wasn't changed: restart failed (500)", edit["text"])

    async def test_without_the_control_service_the_button_is_hidden_and_the_manual_command_shown(self):
        config.WARP_CONTROL_TOKEN = ""
        edit = await self.tap("adm|warp")
        self.assertEqual(labels(edit["markup"]), ["↻ Refresh", "← Back"])
        self.assertIn("force-recreate warp", edit["text"])

    async def test_an_unreachable_proxy_is_said_so(self):
        main.tcp_alive = lambda proxy: False
        edit = await self.tap("adm|warp")
        self.assertIn("✕ not reachable", edit["text"])
        self.assertNotIn("<code>1.1.1.1</code>", edit["text"])

    async def test_nobody_else_can_use_it(self):
        edit = await self.tap("adm|warp_go", user=7)
        self.assertIn("Not authorized", edit["text"])
        self.assertEqual(self.posts, [])


# ---------------------------------------------------------------- the control service, against a fake Docker
def load_control(token="secret"):
    path = Path(__file__).resolve().parents[2] / "warp_control" / "control.py"
    import os
    with mock.patch.dict(os.environ, {"WARP_CONTROL_TOKEN": token, "WARP_READY_TIMEOUT": "5"}):
        spec = importlib.util.spec_from_file_location("warp_control_service", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class Service(unittest.TestCase):
    def setUp(self):
        self.ctl = load_control()
        self.calls = []
        self.addCleanup(setattr, self.ctl.time, "sleep", self.ctl.time.sleep)     # ctl.time IS the stdlib module: restore it
        self.ctl.time.sleep = lambda s: None
        self.status_outputs = ["Disconnected", "Status update: Connected"]
        self.restart_status = 204

        def run(*cmd):
            self.calls.append(cmd)
            if cmd[-1] == "status":
                return 0, (self.status_outputs.pop(0) if len(self.status_outputs) > 1 else self.status_outputs[0])
            return 0, ""

        def docker(method, path, body=None):
            self.calls.append((method, path))
            return (self.restart_status, b"")
        self.ctl.run_in_container, self.ctl.docker = run, docker

    def test_it_refuses_to_exist_without_a_token_and_checks_it_in_constant_time(self):
        self.assertTrue(self.ctl.authorized("Bearer secret"))
        for bad in (None, "", "secret", "Bearer wrong", "Bearer "):
            self.assertFalse(self.ctl.authorized(bad), bad)
        self.assertFalse(load_control("").authorized("Bearer "))

    def test_a_rotation_forgets_the_registration_restarts_and_waits_for_connected(self):
        result = self.ctl.rotate()
        self.assertEqual(result, {"ok": True, "detail": "connected"})
        order = [c for c in self.calls if isinstance(c, tuple)]
        self.assertEqual(order[0][-2:], ("registration", "delete"))
        self.assertIn(("POST", "/containers/candy_warp/restart?t=5"), self.calls)
        self.assertLess(self.calls.index(order[0]), self.calls.index(("POST", "/containers/candy_warp/restart?t=5")))

    def test_a_failed_restart_is_reported(self):
        self.restart_status = 500
        self.assertEqual(self.ctl.rotate()["ok"], False)

    def test_if_it_never_comes_back_that_is_reported_and_it_registers_by_hand_once(self):
        self.status_outputs = ["Disconnected"]
        self.ctl.READY_TIMEOUT = 60
        clock = iter(range(0, 1000, 4))
        self.addCleanup(setattr, self.ctl.time, "time", self.ctl.time.time)
        self.ctl.time.time = lambda: next(clock)
        result = self.ctl.rotate()
        self.assertFalse(result["ok"])
        self.assertEqual(sum(1 for c in self.calls if c[-2:] == ("registration", "new")), 1)

    def test_only_one_rotation_at_a_time(self):
        self.ctl._lock.acquire()
        self.addCleanup(self.ctl._lock.release)
        self.assertTrue(self.ctl._lock.locked())


if __name__ == "__main__":
    unittest.main()
