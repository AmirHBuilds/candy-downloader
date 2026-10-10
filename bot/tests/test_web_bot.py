"""The bot side of web accounts, the RoutingBot, server startup, live updates, and that the page only calls routes that exist."""
import asyncio
import json
import re
from pathlib import Path
from unittest import mock

from tests.webharness import PASSWORD, Client, FakeTelegramBot, WebBase  # noqa: E402  (imports _env first)

import config  # noqa: E402
import main  # noqa: E402
from utils import webchat  # noqa: E402
from utils.webchat import RoutingBot, chat_id_for, deliveries, is_web_chat, remember_job  # noqa: E402
from web import accounts, botcmds, server, shares  # noqa: E402


class Routing(WebBase):
    quick_login = False

    def test_web_chat_ids_never_look_like_telegram_ones(self):
        for tg in (1, 123456789, 7_000_000_000, -1001234567890, -4_000_000_000_000_000):
            self.assertFalse(is_web_chat(tg), tg)
        for acc in (1, 2, 10 ** 6):
            self.assertTrue(is_web_chat(chat_id_for(acc)))
        self.assertFalse(is_web_chat(None))
        self.assertFalse(is_web_chat("123"))
        self.assertEqual(webchat.account_of_chat(chat_id_for(42)), 42)

    async def test_real_chats_pass_through_untouched(self):
        calls = []

        class Real:
            token = "abc"

            async def send_message(self, chat_id, text, **kw):
                calls.append(("send", chat_id, text, kw))
                return "sent"

            async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
                calls.append(("edit", chat_id, text))
                return "edited"

            async def get_me(self):
                return "me"
        bot = RoutingBot(Real())
        self.assertEqual(await bot.send_message(7, "hi", parse_mode="HTML"), "sent")
        self.assertEqual(await bot.edit_message_text("t", chat_id=7, message_id=1), "edited")
        self.assertEqual(await bot.get_me(), "me")
        self.assertEqual(bot.token, "abc")
        self.assertEqual(calls[0], ("send", 7, "hi", {"parse_mode": "HTML"}))

    async def test_web_chats_never_reach_telegram(self):
        calls = []

        class Real:
            def __getattr__(self, name):
                async def record(*a, **k):
                    calls.append(name)
                return record
        bot = RoutingBot(Real())
        chat = chat_id_for(3)
        for coro in (bot.send_message(chat, "x"), bot.send_document(chat, object()), bot.send_video(chat, object()),
                     bot.delete_message(chat, 5), bot.edit_message_text("t", chat_id=chat, message_id=0),
                     bot.edit_message_caption(chat_id=chat, message_id=0, caption="c")):
            self.assertIsNone(await coro)
        self.assertEqual(calls, [])

    async def test_messages_for_a_web_chat_become_notes_of_that_job(self):
        bot = RoutingBot(FakeTelegramBot())
        chat = chat_id_for(3)
        remember_job(chat, "jobA")
        await bot.send_message(chat, "Subtitles didn't work")
        await bot.send_message(chat, text="again")
        self.assertEqual(deliveries.notes["jobA"], ["Subtitles didn't work", "again"])
        self.assertEqual(bot._real.messages, [])


class BotCommands(WebBase):
    quick_login = False

    def test_admin_commands(self):
        self.assertIn("/webaccount add", botcmds.admin_command([]))
        reply = botcmds.admin_command(["add", "newbie", "777"], "https://dl.example.com")
        pw = re.search(r"Password: <code>(\S+)</code>", reply).group(1)
        self.assertIn("https://dl.example.com", reply)
        acc = accounts.authenticate("newbie", pw)
        self.assertEqual((acc.telegram_id, acc.role, acc.must_change), (777, "user", True))
        self.assertEqual(accounts.account_by_telegram(777).username, "newbie")
        self.assertIn("newbie", botcmds.admin_command(["list"]))
        pw2 = re.search(r"Password: <code>(\S+)</code>", botcmds.admin_command(["reset", "NEWBIE"])).group(1)
        self.assertIsNone(accounts.authenticate("newbie", pw))
        self.assertIsNotNone(accounts.authenticate("newbie", pw2))
        self.assertIn("blocked", botcmds.admin_command(["off", "newbie"]))
        self.assertIsNone(accounts.authenticate("newbie", pw2))
        self.assertIn("allowed", botcmds.admin_command(["on", "newbie"]))
        self.assertIn("Deleted", botcmds.admin_command(["del", "newbie"]))
        self.assertIn("No account", botcmds.admin_command(["del", "newbie"]))

    def test_admin_role_flag_and_errors(self):
        reply = botcmds.admin_command(["add", "boss2", "admin"])
        self.assertEqual([a.role for a in accounts.list_accounts() if a.username == "boss2"], ["admin"])
        self.assertIn("✕", botcmds.admin_command(["add", "x"]))                          # too short a name
        self.assertIn("taken", botcmds.admin_command(["add", "alice"]))
        self.assertIn("at least one active admin", botcmds.admin_command(["del", "boss2"]) + botcmds.admin_command(["del", "alice"]))
        self.assertIn("/webaccount", botcmds.admin_command(["add"]))
        self.assertIn("/webaccount", botcmds.admin_command(["frobnicate", "x"]))

    def test_credentials_text_escapes(self):
        acc = type("A", (), {"username": "<b>x"})()
        text = botcmds.credentials_text(acc, "p<w>", True, "https://a/?x=1&y=2")
        self.assertNotIn("<b>x", text)
        self.assertIn("&lt;b&gt;x", text)
        self.assertIn("&amp;", text)

    def update(self, user_id=5, chat_type="private", username="cool_user", args=()):
        replies = []

        class Message:
            async def reply_text(s, text, **kw):
                replies.append(text)
                return type("S", (), {"chat_id": 1, "message_id": len(replies)})()

            async def delete(s):
                pass
        update = type("U", (), {"message": Message(), "effective_user": type("X", (), {"id": user_id, "username": username})(),
                                "effective_chat": type("C", (), {"type": chat_type, "id": 1})()})()
        context = type("Ctx", (), {"bot": FakeTelegramBot(), "args": list(args)})()
        return update, context, replies

    async def run_cmd(self, fn, *a, **kw):
        update, context, replies = self.update(*a, **kw)
        with mock.patch.object(main, "_delete_later", mock.AsyncMock()):
            await fn(update, context)
            await asyncio.sleep(0)
        return replies

    async def test_weblogin_gives_a_working_login_tied_to_the_telegram_user(self):
        with mock.patch.object(config, "WEB_ENABLED", True):
            replies = await self.run_cmd(main.weblogin_cmd, user_id=555)
        match = re.search(r"Username: <code>(\S+)</code>\nPassword: <code>(\S+)</code>", replies[0])
        acc = accounts.authenticate(match.group(1), match.group(2))
        self.assertEqual((acc.telegram_id, acc.role, acc.must_change), (555, "user", True))

    async def test_weblogin_for_the_owner_makes_an_admin(self):
        with mock.patch.object(config, "WEB_ENABLED", True), mock.patch.object(config, "OWNER_USER_ID", 555):
            await self.run_cmd(main.weblogin_cmd, user_id=555)
        self.assertEqual(accounts.account_by_telegram(555).role, "admin")

    async def test_weblogin_refuses_groups_and_when_the_web_is_off(self):
        with mock.patch.object(config, "WEB_ENABLED", True):
            replies = await self.run_cmd(main.weblogin_cmd, user_id=556, chat_type="group")
        self.assertIn("private", replies[0])
        self.assertIsNone(accounts.account_by_telegram(556))
        with mock.patch.object(config, "WEB_ENABLED", False):
            replies = await self.run_cmd(main.weblogin_cmd, user_id=557)
        self.assertIn("isn't switched on", replies[0])
        self.assertIsNone(accounts.account_by_telegram(557))

    async def test_weblogin_respects_private_mode(self):
        from settings import access_control as ac
        ac.set_mode("private")
        with mock.patch.object(config, "WEB_ENABLED", True):
            replies = await self.run_cmd(main.weblogin_cmd, user_id=558)
        self.assertIsNone(accounts.account_by_telegram(558))

    async def test_webaccount_is_for_admins_only(self):
        with mock.patch.object(config, "OWNER_USER_ID", 1), mock.patch.object(config, "ADMIN_USER_IDS", {2}):
            self.assertEqual(await self.run_cmd(main.webaccount_cmd, user_id=99, args=["add", "intruder"]), [])
            self.assertEqual([a.username for a in accounts.list_accounts() if a.username == "intruder"], [])
            replies = await self.run_cmd(main.webaccount_cmd, user_id=2, args=["add", "legit"])
            self.assertIn("Password:", replies[0])
            replies = await self.run_cmd(main.webaccount_cmd, user_id=1, args=["list"])
            self.assertIn("legit", replies[0])
            replies = await self.run_cmd(main.webaccount_cmd, user_id=1, args=["add", "grp"], chat_type="supergroup")
            self.assertIn("private chat", replies[0])
            self.assertNotIn("grp", [a.username for a in accounts.list_accounts()])


class Startup(WebBase):
    quick_login = False

    async def test_bootstrap_admin_and_server_start(self):
        accounts_before = len(accounts.list_accounts())
        with mock.patch.object(config, "WEB_ADMIN_USER", "ownerweb"), mock.patch.object(config, "WEB_ADMIN_PASSWORD", "owner long password"), \
                mock.patch.object(server._Server, "serve", mock.AsyncMock()):
            tasks = await server.start(self.manager, self.tg)
        for t in tasks:
            t.cancel()
        self.assertEqual(accounts_before, len(accounts.list_accounts()))      # alice already is an admin: nothing is created

    async def test_startup_sweeps_expired_links(self):
        import time
        src = self.tmp / "tmp" / "s.bin"
        src.write_bytes(b"x")
        old = shares.create(9, src, "old.bin", hours=1)
        with mock.patch("web.shares.time.time", return_value=time.time() + 7200), \
                mock.patch.object(server._Server, "serve", mock.AsyncMock()):
            tasks = await server.start(self.manager, self.tg)
            await asyncio.sleep(0.3)
        for t in tasks:
            t.cancel()
        self.assertFalse(old.path().parent.exists())

    async def test_first_start_creates_the_admin_from_env(self):
        for acc in accounts.list_accounts():
            accounts.delete_account(acc.id) if acc.role != "admin" else None
        import sqlite3
        conn = sqlite3.connect(accounts.DB_PATH)
        conn.execute("DELETE FROM web_accounts")
        conn.commit()
        conn.close()
        with mock.patch.object(config, "WEB_ADMIN_USER", "ownerweb"), mock.patch.object(config, "WEB_ADMIN_PASSWORD", "owner long password"), \
                mock.patch.object(server._Server, "serve", mock.AsyncMock()):
            tasks = await server.start(self.manager, self.tg)
        for t in tasks:
            t.cancel()
        acc = accounts.authenticate("ownerweb", "owner long password")
        self.assertEqual((acc.role, acc.must_change), ("admin", False))

    async def test_a_weak_env_password_does_not_crash_startup(self):
        import sqlite3
        conn = sqlite3.connect(accounts.DB_PATH)
        conn.execute("DELETE FROM web_accounts")
        conn.commit()
        conn.close()
        with mock.patch.object(config, "WEB_ADMIN_USER", "ownerweb"), mock.patch.object(config, "WEB_ADMIN_PASSWORD", "weak"), \
                mock.patch.object(server._Server, "serve", mock.AsyncMock()):
            tasks = await server.start(self.manager, self.tg)
        for t in tasks:
            t.cancel()
        self.assertEqual(accounts.list_accounts(), [])

    async def test_send_to_telegram_uploads_the_file(self):
        class Bot(FakeTelegramBot):
            async def send_document(s, chat_id, fh, **kw):
                s.messages.append((chat_id, fh.read(), kw["filename"]))
        bot = Bot()
        captured = {}
        real = webapp_context = None
        from web import app as webapp
        original = webapp.Context

        def spy(jobs, send_to_telegram=None, **kw):
            captured["send"] = send_to_telegram
            return original(jobs, send_to_telegram=send_to_telegram, **kw)
        with mock.patch.object(webapp, "Context", spy), mock.patch.object(server, "Context", spy), \
                mock.patch.object(server._Server, "serve", mock.AsyncMock()):
            tasks = await server.start(self.manager, bot)
        for t in tasks:
            t.cancel()
        f = self.tmp / "f.bin"
        f.write_bytes(b"data")
        await captured["send"](42, f, "f.bin")
        self.assertEqual(bot.messages, [(42, b"data", "f.bin")])

    async def test_bot_wires_the_routing_bot_and_web_only_when_enabled(self):
        src = Path("main.py").read_text()
        self.assertIn("JobManager(RoutingBot(application.bot)", src)
        self.assertIn("if config.WEB_ENABLED:", src)
        self.assertIn('CommandHandler("weblogin"', src)
        self.assertIn('CommandHandler("webaccount"', src)
        self.assertIn(r'pattern=r"^up\|"', src)


class Live(WebBase):
    async def test_stream_sends_job_updates(self):
        self.fake_download(b"x", "a.mp3")
        events, disconnect = [], asyncio.Event()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
                 "path": "/api/jobs/stream", "raw_path": b"/api/jobs/stream", "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"web.test"), (b"cookie", "; ".join(f"{k}={v}" for k, v in self.b.cookies.items()).encode())],
                 "client": ("127.0.0.1", 1), "server": ("web.test", 443)}
        started = {}

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                started["status"], started["headers"] = message["status"], dict(message["headers"])
            elif message["type"] == "http.response.body":
                events.append(message.get("body", b"").decode())
        task = asyncio.create_task(self.app(scope, receive, send))
        await asyncio.sleep(0.3)
        await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})
        for _ in range(60):
            if any('"state": "done"' in e for e in events):
                break
            await asyncio.sleep(0.1)
        disconnect.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(started["status"], 200)
        self.assertEqual(started["headers"][b"content-type"], b"text/event-stream; charset=utf-8")
        self.assertEqual(started["headers"][b"cache-control"], b"no-store")
        joined = "".join(events)
        self.assertIn("event: jobs", joined)
        self.assertIn('"state": "done"', joined)
        self.assertEqual(self.ctx.streams.get(self.bob.id, 0), 0)                 # the counter is released on disconnect

    async def test_stream_needs_login_and_is_limited_per_account(self):
        self.assertEqual((await Client(self.app).get("/api/jobs/stream")).status, 401)
        self.ctx.streams[self.bob.id] = 4
        self.assertEqual((await self.b.get("/api/jobs/stream")).status, 429)

    async def test_stream_only_carries_my_own_jobs(self):
        self.fake_download()
        await self.a.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})
        self.assertEqual((await self.b.get("/api/jobs")).json()["jobs"], [])


class PageMatchesApi(WebBase):
    quick_login = False

    def test_every_api_path_the_page_uses_exists(self):
        js = Path("web/static/app.js").read_text()
        used = set(re.findall(r"""["'`](/api/[^"'`?]*)""", js))
        routes = [getattr(r, "path", "") for r in self.app.app.routes]
        patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r) + "$") for r in routes if r.startswith("/api/")]
        for path in sorted(used):
            normalised = re.sub(r"\$\{[^}]+\}", "x", path)
            self.assertTrue(any(p.match(normalised) for p in patterns) or any(p.match(normalised.rstrip("/") + "/x") for p in patterns),
                            f"the page calls {path}, which has no route")

    def test_the_page_sends_the_csrf_token_on_every_state_change(self):
        js = Path("web/static/app.js").read_text()
        self.assertIn('init.headers["X-CSRF-Token"] = state.csrf', js)
        self.assertIn('xhr.setRequestHeader("X-CSRF-Token", state.csrf)', js)
        self.assertNotIn("localStorage.setItem(\"token", js)
        self.assertNotIn("document.cookie", js)

    def test_every_post_put_delete_route_is_covered_by_the_csrf_check(self):
        src = Path("web/app.py").read_text()
        for route in re.findall(r'Route\("([^"]+)", (\w+), methods=\[("(?:POST|PUT|PATCH|DELETE)")', src):
            path, handler, _ = route
            body = src[src.index(f"async def {handler}("):]
            decorator = body.split("async def")[0] if False else src[:src.index(f"async def {handler}(")].rsplit("@ep", 1)[1].split("\n")[0]
            self.assertNotIn("csrf=False", decorator, path)
            if path != "/api/auth/login":
                self.assertNotIn("auth=False", decorator, path)


if __name__ == "__main__":
    import unittest
    unittest.main()
