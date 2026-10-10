"""Built-in download links: the store (web/shares.py), the public pages, the web API, and the bot's button and /links."""
import time
from pathlib import Path
from unittest import mock

from tests.webharness import Client, WebBase  # noqa: E402  (imports _env first)

import main  # noqa: E402
from web import accounts, shares  # noqa: E402


class Store(WebBase):
    quick_login = False

    def src(self, content=b"data", name="a.bin"):
        path = self.tmp / "tmp" / name
        path.write_bytes(content)
        return path

    def test_create_copies_the_file_and_finds_it_by_token(self):
        share = shares.create(7, self.src(b"hello"), "My Song.mp3", hours=2)
        self.assertEqual(shares.find(share.token).name, "My Song.mp3")
        self.assertEqual(shares.find(share.token).path().read_bytes(), b"hello")
        self.assertAlmostEqual(share.expires - share.created, 7200, delta=2)
        self.assertEqual(len(share.token), 43)

    def test_the_original_can_vanish_without_breaking_the_link(self):
        src = self.src(b"keep me")
        share = shares.create(7, src, "x.bin")
        src.unlink()
        self.assertEqual(shares.find(share.token).path().read_bytes(), b"keep me")

    def test_tokens_are_validated_before_touching_the_disk(self):
        for token in ("", "..", "../../etc/passwd", "a" * 42, "a" * 44, "a/" + "b" * 41, None, 5):
            self.assertIsNone(shares.find(token), repr(token))

    def test_malformed_tokens_never_reach_the_database(self):
        with mock.patch.object(shares, "_connect", side_effect=AssertionError("database touched")):
            for token in ("../../etc/passwd", "a" * 42, "x y" + "a" * 40, "", None):
                self.assertIsNone(shares.find(token), repr(token))

    def test_names_cannot_escape_or_be_empty(self):
        share = shares.create(7, self.src(), "../../evil\\x/../name.txt")
        self.assertNotIn("/", share.name)
        self.assertNotIn("\\", share.name)
        self.assertEqual(shares.create(7, self.src(), "   ").name, "file")
        self.assertTrue(share.path().resolve().is_relative_to(shares.share_root().resolve()))

    def test_duration_is_clamped_to_the_admin_maximum(self):
        shares.set_config({"max_hours": 10, "default_hours": 5})
        share = shares.create(7, self.src(), "x", hours=500)
        self.assertAlmostEqual(share.expires - share.created, 36000, delta=2)
        self.assertAlmostEqual(shares.create(7, self.src(), "x").expires - time.time(), 18000, delta=5)
        for bad in (0, -3, "5", True, 1.5):
            with self.assertRaises(shares.ShareError):
                shares.create(7, self.src(), "x", hours=bad)

    def test_quota_counts_per_owner(self):
        shares.set_config({"user_quota_mb": 1})
        shares.create(7, self.src(b"x" * 600_000), "a")
        with self.assertRaises(shares.ShareError):
            shares.create(7, self.src(b"x" * 600_000), "b")
        shares.create(8, self.src(b"x" * 600_000), "c")                       # someone else's allowance is separate

    def test_download_limit_and_expiry_make_a_link_dead(self):
        once = shares.create(7, self.src(), "x", max_downloads=1)
        self.assertIsNotNone(shares.find(once.token))
        shares.count_download(once.id)
        self.assertIsNone(shares.find(once.token))
        old = shares.create(7, self.src(), "y", hours=1)
        with mock.patch("web.shares.time.time", return_value=time.time() + 7200):
            self.assertIsNone(shares.find(old.token))
        for bad in (-1, 1001, "1", True):
            with self.assertRaises(shares.ShareError):
                shares.create(7, self.src(), "z", max_downloads=bad)

    def test_delete_is_owner_checked_and_removes_the_file(self):
        share = shares.create(7, self.src(), "x")
        self.assertFalse(shares.delete(share.id, owner=8))
        self.assertTrue(share.path().is_file())
        self.assertTrue(shares.delete(share.id, owner=7))
        self.assertFalse(share.path().parent.exists())
        self.assertIsNone(shares.find(share.token))

    def test_sweep_removes_dead_links_and_orphan_folders_but_keeps_live_ones(self):
        live = shares.create(7, self.src(), "live", hours=5)
        dead = shares.create(7, self.src(), "dead", hours=1)
        orphan = shares.share_root() / ("o" * 43)
        orphan.mkdir(parents=True)
        (orphan / "file").write_text("x")
        with mock.patch("web.shares.time.time", return_value=time.time() + 7200):
            shares.sweep()
        self.assertTrue(live.path().is_file())
        self.assertFalse(dead.path().parent.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual([s.name for s in shares.list_for(7)], ["live"])

    def test_config_validation_and_switch(self):
        for bad in ({"enabled": "yes"}, {"default_hours": 0}, {"max_hours": "5"}, {"user_quota_mb": True},
                    {"default_hours": 10 ** 9}):
            with self.assertRaises(shares.ShareError):
                shares.set_config(bad)
        shares.set_config({"enabled": False})
        with self.assertRaises(shares.ShareError):
            shares.create(7, self.src(), "x")
        self.assertFalse(shares.available())
        shares.set_config({"enabled": True})
        self.assertTrue(shares.available())

    def test_available_needs_the_web_server(self):
        with mock.patch.object(shares.app_config, "WEB_ENABLED", False):
            self.assertFalse(shares.available())

    def test_base_url_prefers_the_public_url_and_falls_back_to_localhost(self):
        with mock.patch.object(shares.app_config, "WEB_PUBLIC_URL", "https://dl.example.com"):
            self.assertEqual(shares.base_url(), "https://dl.example.com")
        with mock.patch.object(shares.app_config, "WEB_PUBLIC_URL", ""), mock.patch.object(shares.app_config, "WEB_PORT", 8080):
            self.assertEqual(shares.base_url(), "http://localhost:8080")


class Http(WebBase):
    async def make(self, client=None, name="Song.mp3", content=b"hello world", **body):
        client = client or self.a
        self.fake_download(content=content, name=name)
        rid = (await client.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(client, rid)
        resp = await client.post(f"/api/files/{rid}/0/link", body)
        return rid, resp

    async def test_make_a_link_and_open_it_without_signing_in(self):
        _, resp = await self.make(hours=3)
        self.assertEqual(resp.status, 200, resp.body)
        path = resp.json()["share"]["path"]
        anon = Client(self.app, ip="192.0.2.77")
        page = await anon.get(path)
        self.assertEqual(page.status, 200)
        self.assertIn(b"Song.mp3", page.body)
        self.assertEqual(page.header("x-robots-tag"), "noindex")
        self.assertEqual(page.header("cache-control"), "no-store")
        direct = await anon.get(resp.json()["share"]["direct"])
        self.assertEqual(direct.status, 200)
        self.assertEqual(direct.body, b"hello world")
        self.assertIn("attachment", direct.header("content-disposition"))
        self.assertEqual(anon.cookies, {})                                        # visiting never starts a session
        self.assertEqual((await self.a.get("/api/shares")).json()["shares"][0]["downloads"], 1)

    async def test_the_file_name_in_the_url_is_cosmetic(self):
        _, resp = await self.make()
        token_path = resp.json()["share"]["path"]
        anon = Client(self.app)
        self.assertEqual((await anon.get(token_path + "/whatever.txt")).body, b"hello world")

    async def test_a_resumed_or_head_request_is_not_a_new_download(self):
        _, resp = await self.make(content=b"x" * 5000)
        anon, direct = Client(self.app), resp.json()["share"]["direct"]
        part = await anon.get(direct, headers={"Range": "bytes=100-"})
        self.assertEqual(part.status, 206)
        self.assertEqual(len(part.body), 4900)
        await anon.request("HEAD", direct)
        self.assertEqual((await self.a.get("/api/shares")).json()["shares"][0]["downloads"], 0)
        await anon.get(direct, headers={"Range": "bytes=0-99"})
        self.assertEqual((await self.a.get("/api/shares")).json()["shares"][0]["downloads"], 1)

    async def test_one_download_only(self):
        _, resp = await self.make(max_downloads=1)
        anon, direct = Client(self.app), resp.json()["share"]["direct"]
        self.assertEqual((await anon.get(direct)).status, 200)
        self.assertEqual((await anon.get(direct)).status, 404)

    async def test_unknown_expired_and_garbage_look_the_same(self):
        _, resp = await self.make()
        shares.delete(shares.list_for(self.alice.user_id)[0].id)
        anon = Client(self.app)
        answers = []
        for path in (resp.json()["share"]["path"], "/s/" + "a" * 43, "/s/short", "/s/..%2f..%2fetc", "/s/" + "a" * 43 + "/x"):
            r = await anon.get(path)
            answers.append((r.status, r.body))
        self.assertEqual({a[0] for a in answers}, {404})
        self.assertEqual(len({a[1] for a in answers}), 1)

    async def test_names_are_escaped_on_the_page(self):
        _, resp = await self.make(name="<img src=x onerror=alert(1)>.mp3")
        page = (await Client(self.app).get(resp.json()["share"]["path"])).body.decode()
        self.assertNotIn("<img", page)
        self.assertIn("&lt;img", page)
        self.assertNotIn("<script", page)

    async def test_public_pages_are_rate_limited_per_address(self):
        anon = Client(self.app, ip="192.0.2.99")
        codes = [(await anon.get("/s/" + "b" * 43)).status for _ in range(300)]
        self.assertIn(429, codes)
        self.assertEqual((await Client(self.app, ip="192.0.2.100").get("/s/" + "b" * 43)).status, 404)

    async def test_the_file_route_is_rate_limited_too(self):
        _, resp = await self.make()
        direct = resp.json()["share"]["direct"]
        anon = Client(self.app, ip="192.0.2.123")
        codes = [(await anon.request("HEAD", direct)).status for _ in range(300)]
        self.assertIn(429, codes)

    async def test_only_the_owner_can_make_list_and_delete(self):
        rid, resp = await self.make()
        sid = resp.json()["share"]["id"]
        self.assertEqual((await self.b.post(f"/api/files/{rid}/0/link", {})).status, 404)
        self.assertEqual((await self.b.get("/api/shares")).json()["shares"], [])
        self.assertEqual((await self.b.delete(f"/api/shares/{sid}")).status, 404)
        for bad in ("abc", "999", "-1"):
            self.assertEqual((await self.a.delete(f"/api/shares/{bad}")).status, 404, bad)
        self.assertEqual((await self.a.delete(f"/api/shares/{sid}")).status, 200)
        self.assertEqual((await Client(self.app).get(resp.json()["share"]["path"])).status, 404)

    async def test_making_and_deleting_needs_sign_in_and_the_csrf_token(self):
        rid, resp = await self.make()
        anon = Client(self.app)
        self.assertEqual((await anon.get("/api/shares")).status, 401)
        self.assertEqual((await anon.post(f"/api/files/{rid}/0/link", {})).status, 401)
        sid = resp.json()["share"]["id"]
        self.assertEqual((await self.a.delete(f"/api/shares/{sid}", csrf=False)).status, 403)
        self.assertEqual((await self.a.post(f"/api/files/{rid}/0/link", {}, origin="https://evil.example")).status, 403)

    async def test_switching_links_off_blocks_new_ones_and_reports_it(self):
        rid, _ = await self.make()
        self.assertEqual((await self.a.put("/api/admin/sharing", {"enabled": False})).status, 200)
        self.assertEqual((await self.a.post(f"/api/files/{rid}/0/link", {})).status, 403)
        self.assertFalse((await self.a.get("/api/me")).json()["sharing"]["available"])
        self.assertFalse((await self.b.get("/api/shares")).json()["available"])

    async def test_admin_manages_settings_and_all_links(self):
        _, resp = await self.make(client=self.b)
        self.assertEqual((await self.b.get("/api/admin/sharing")).status, 403)
        self.assertEqual((await self.b.put("/api/admin/sharing", {"enabled": False})).status, 403)
        self.assertEqual((await self.b.delete("/api/admin/shares/1")).status, 403)
        info = (await self.a.get("/api/admin/sharing")).json()
        self.assertEqual(info["count"], 1)
        self.assertEqual(info["shares"][0]["owner"], self.bob.user_id)
        self.assertNotIn("token", str(info["shares"][0]).replace("'path'", ""))   # only /s/<token> paths, nothing extra
        self.assertEqual((await self.a.put("/api/admin/sharing", {"default_hours": 0})).status, 400)
        self.assertEqual((await self.a.put("/api/admin/sharing", {"default_hours": 12, "max_hours": 48})).json()["default_hours"], 12)
        self.assertEqual((await self.a.delete(f"/api/admin/shares/{resp.json()['share']['id']}")).status, 200)
        self.assertEqual((await self.a.get("/api/admin/sharing")).json()["count"], 0)

    async def test_deleting_a_web_only_account_removes_its_links(self):
        _, resp = await self.make(client=self.b)
        path = Path(shares.list_for(self.bob.user_id)[0].path())
        self.assertTrue(path.is_file())
        self.assertEqual((await self.a.delete(f"/api/admin/accounts/{self.bob.id}")).status, 200)
        self.assertFalse(path.exists())
        self.assertEqual((await Client(self.app).get(resp.json()["share"]["path"])).status, 404)

    async def test_a_telegram_linked_account_sees_the_links_made_in_the_bot(self):
        tg = accounts.create_account("tgz", "long enough pw 1", telegram_id=5151)
        client = Client(self.app, ip="198.51.100.50")
        await client.login("tgz", "long enough pw 1")
        src = self.tmp / "tmp" / "q.bin"
        src.write_bytes(b"1")
        shares.create(5151, src, "from-bot.bin")
        self.assertEqual([s["name"] for s in (await client.get("/api/shares")).json()["shares"]], ["from-bot.bin"])
        self.assertEqual(tg.user_id, 5151)

    async def test_security_headers_are_on_public_pages_too(self):
        _, resp = await self.make()
        page = await Client(self.app).get(resp.json()["share"]["path"])
        self.assertIn("default-src 'none'", page.header("content-security-policy"))
        self.assertEqual(page.header("x-content-type-options"), "nosniff")
        self.assertEqual(page.header("referrer-policy"), "no-referrer")


class BotButtons(WebBase):
    quick_login = False

    def update(self, user_id=42, data="up|link|rid1"):
        query = mock.AsyncMock()
        query.data = data
        query.message.chat_id = 99
        update = mock.Mock(callback_query=query, effective_user=mock.Mock(id=user_id))
        context = mock.Mock(bot=mock.AsyncMock())
        return update, context, query

    async def run_cb(self, files, **kw):
        update, context, query = self.update(**kw)
        with mock.patch.object(main, "gate_callback", mock.AsyncMock(return_value=True)), \
                mock.patch.object(main, "job_manager", mock.Mock(cached_files=mock.Mock(return_value=files))):
            await main.uploader_callback(update, context)
        return context, query

    async def test_get_a_link_makes_links_for_the_files(self):
        path = self.tmp / "tmp" / "v.mp4"
        path.write_bytes(b"video")
        with mock.patch.object(shares.app_config, "WEB_PUBLIC_URL", "https://dl.example.com"):
            context, query = await self.run_cb([{"path": path, "name": "v <1>.mp4"}])
        text = context.bot.send_message.await_args.args[1]
        self.assertIn("https://dl.example.com/s/", text)
        self.assertIn("v &lt;1&gt;.mp4", text)
        self.assertEqual(len(shares.list_for(42)), 1)
        self.assertEqual(shares.list_for(42)[0].path().read_bytes(), b"video")

    async def test_nothing_kept_or_switched_off_says_so_and_makes_nothing(self):
        context, query = await self.run_cb([])
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))
        context.bot.send_message.assert_not_awaited()
        path = self.tmp / "tmp" / "v.mp4"
        path.write_bytes(b"v")
        shares.set_config({"enabled": False})
        context, query = await self.run_cb([{"path": path, "name": "v.mp4"}])
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(shares.list_for(42), [])

    async def test_a_quota_error_is_shown_not_raised(self):
        shares.set_config({"user_quota_mb": 1})
        path = self.tmp / "tmp" / "big.bin"
        path.write_bytes(b"x" * 1_100_000)
        context, _ = await self.run_cb([{"path": path, "name": "big.bin"}])
        self.assertIn("✕", context.bot.send_message.await_args.args[1])

    async def test_links_command_lists_and_the_delete_button_only_deletes_your_own(self):
        path = self.tmp / "tmp" / "v.mp4"
        path.write_bytes(b"v")
        mine = shares.create(42, path, "mine.mp4")
        theirs = shares.create(43, path, "theirs.mp4")
        text, markup = main._links_view(42)
        self.assertIn("mine.mp4", text)
        self.assertNotIn("theirs.mp4", text)
        update, context, query = self.update(user_id=42, data=f"ul|{theirs.id}")
        with mock.patch.object(main, "gate_callback", mock.AsyncMock(return_value=True)):
            await main.links_callback(update, context)
        self.assertEqual(len(shares.list_for(43)), 1)
        update, context, query = self.update(user_id=42, data=f"ul|{mine.id}")
        with mock.patch.object(main, "gate_callback", mock.AsyncMock(return_value=True)):
            await main.links_callback(update, context)
        self.assertEqual(shares.list_for(42), [])
        self.assertIn("no active links", query.edit_message_text.await_args.args[0])

    async def test_a_garbled_delete_button_does_nothing(self):
        update, context, query = self.update(data="ul|xyz")
        with mock.patch.object(main, "gate_callback", mock.AsyncMock(return_value=True)):
            await main.links_callback(update, context)
        query.edit_message_text.assert_not_awaited()


class Access(WebBase):
    """Who may open a link (anyone / signed-in / only the maker) and the optional password."""

    async def make(self, client=None, **body):
        client = client or self.a
        self.fake_download(content=b"secret bytes", name="Private.mp4")
        rid = (await client.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(client, rid)
        resp = await client.post(f"/api/files/{rid}/0/link", body)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()["share"]

    async def test_a_signed_in_only_link_turns_strangers_away_without_naming_the_file(self):
        share = await self.make(access=1)
        anon = Client(self.app, ip="192.0.2.8")
        for path in (share["path"], share["direct"]):
            r = await anon.get(path)
            self.assertEqual(r.status, 401, path)
            self.assertNotIn(b"Private.mp4", r.body)
            self.assertNotIn(b"secret bytes", r.body)
        self.assertEqual((await self.b.get(share["path"])).status, 200)           # bob has an account
        self.assertEqual((await self.b.get(share["direct"])).body, b"secret bytes")
        self.assertEqual((await self.a.get("/api/shares")).json()["shares"][0]["downloads"], 1)   # the refusals didn't count

    async def test_arriving_from_another_app_gets_a_continue_button_that_works(self):
        share = await self.make(access=1)
        refusal = await Client(self.app).get(share["path"])
        self.assertIn(f'href="{share["path"]}"'.encode(), refusal.body)           # same-site click: the cookie is sent then
        self.assertIn(b'href="/"', refusal.body)
        self.assertEqual((await self.b.get(share["path"])).status, 200)

    async def test_the_defaults_shown_never_sit_below_the_minimum(self):
        shares.set_config({"default_access": 0, "min_access": 1})
        self.assertEqual(shares.get_config()["default_access"], 1)

    async def test_an_unlock_expires_after_an_hour(self):
        import time
        share = await self.make(password="open sesame")
        anon = Client(self.app, ip="192.0.2.61")
        await anon.post(share["path"] + "/download", body=b"password=open+sesame",
                        headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual((await anon.get(share["direct"])).status, 200)
        with mock.patch("web.app.time.time", return_value=time.time() + 3700):
            self.assertEqual((await anon.get(share["direct"])).status, 401)

    async def test_an_only_me_link_is_for_the_maker_alone(self):
        share = await self.make(access=2)
        self.assertEqual((await self.a.get(share["direct"])).body, b"secret bytes")
        self.assertEqual((await self.b.get(share["direct"])).status, 401)
        self.assertEqual((await Client(self.app).get(share["direct"])).status, 401)

    async def test_a_blocked_or_signed_out_account_loses_access(self):
        share = await self.make(access=1)
        self.assertEqual((await self.b.get(share["direct"])).status, 200)
        accounts.set_disabled(self.bob.id, True)
        self.assertEqual((await self.b.get(share["direct"])).status, 401)
        await self.b.post("/api/auth/logout")
        accounts.set_disabled(self.bob.id, False)
        self.assertEqual((await self.b.get(share["direct"])).status, 401)

    async def test_default_and_minimum_access_are_set_by_the_admin(self):
        self.assertEqual((await self.a.put("/api/admin/sharing", {"default_access": 1})).json()["default_access"], 1)
        self.assertEqual((await self.make())["access"], 1)                        # nothing chosen: the admin's default
        self.assertEqual((await self.make(access=0))["access"], 0)
        self.assertEqual((await self.a.put("/api/admin/sharing", {"min_access": 1})).json()["min_access"], 1)
        self.assertEqual((await self.make(access=0))["access"], 1)                # can't be looser than the minimum
        self.assertEqual((await self.b.get("/api/shares")).json()["min_access"], 1)
        for bad in (3, -1, "1", True):
            self.assertEqual((await self.a.put("/api/admin/sharing", {"min_access": bad})).status, 400, bad)

    async def test_a_password_link_hides_everything_until_unlocked(self):
        share = await self.make(password="open sesame")
        anon = Client(self.app, ip="192.0.2.20")
        for path in (share["path"], share["direct"]):
            r = await anon.get(path)
            self.assertIn(b'type="password"', r.body, path)
            self.assertNotIn(b"Private.mp4", r.body)
            self.assertNotIn(b"secret bytes", r.body)
        self.assertEqual((await anon.get(share["direct"])).status, 401)
        wrong = await anon.post(share["path"] + "/download", body=b"password=nope", headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(wrong.status, 401)
        self.assertIn(b"isn&#x27;t the password", wrong.body)
        self.assertEqual(anon.cookies, {})
        ok = await anon.post(share["direct"], body=b"password=open+sesame", headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(ok.status, 303)
        cookie = ok.set_cookies()[0]
        for flag in ("HttpOnly", "SameSite=strict", f"Path={share['path']}"):
            self.assertIn(flag, cookie)
        self.assertIn(b"Private.mp4", (await anon.get(share["path"])).body)        # now the page names the file
        self.assertEqual((await anon.get(share["direct"])).body, b"secret bytes")
        self.assertEqual((await anon.get(share["direct"], headers={"Range": "bytes=2-"})).status, 206)
        self.assertEqual((await self.a.get("/api/shares")).json()["shares"][0]["downloads"], 1)

    async def test_an_unlock_only_opens_its_own_link_and_ends_when_the_password_changes(self):
        one, two = await self.make(password="first pass"), await self.make(password="second pass")
        anon = Client(self.app, ip="192.0.2.21")
        form = {"content-type": "application/x-www-form-urlencoded"}
        await anon.post(one["direct"], body=b"password=first+pass", headers=form)
        grant = anon.cookies["candy_dl"]
        other = Client(self.app, ip="192.0.2.22")
        other.cookies["candy_dl"] = grant
        self.assertEqual((await other.get(two["direct"])).status, 401)           # one link's key doesn't fit another
        self.assertEqual((await other.get(one["direct"])).status, 200)
        self.assertEqual((await self.a.patch(f"/api/shares/{one['id']}", {"password": "brand new"})).status, 200)
        self.assertEqual((await other.get(one["direct"])).status, 401)           # changing the password revokes unlocks

    async def test_guessing_a_link_password_is_throttled(self):
        share = await self.make(password="right one")
        form = {"content-type": "application/x-www-form-urlencoded"}
        anon = Client(self.app, ip="192.0.2.30")
        codes = [(await anon.post(share["direct"], body=b"password=guess%d" % i, headers=form)).status for i in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])
        self.assertEqual((await anon.post(share["direct"], body=b"password=right+one", headers=form)).status, 429)
        for i in range(6):                                                         # many addresses can't grind either
            other = Client(self.app, ip=f"192.0.2.{40 + i}")
            for j in range(5):
                await other.post(share["direct"], body=b"password=x%d" % j, headers=form)
        fresh = Client(self.app, ip="192.0.2.99")
        self.assertEqual((await fresh.post(share["direct"], body=b"password=right+one", headers=form)).status, 429)

    async def test_oversized_password_forms_are_refused(self):
        share = await self.make(password="right one")
        r = await Client(self.app).post(share["direct"], body=b"password=" + b"a" * 5000,
                                        headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 413)

    async def test_the_password_is_hashed_and_never_returned(self):
        share = await self.make(password="plain words here")
        self.assertTrue(share["has_password"])
        row = shares.get(share["id"])
        self.assertTrue(row.pw_hash.startswith("scrypt$"))
        self.assertNotIn("plain words here", row.pw_hash)
        for path in ("/api/shares", "/api/admin/sharing"):
            body = (await self.a.get(path)).body.decode()
            self.assertNotIn("scrypt", body)
            self.assertNotIn("plain words", body)
        for bad in ("abc", "x" * 101, 5, ["a"]):
            with self.assertRaises(shares.ShareError):
                shares._clean_password(bad)

    async def test_the_maker_can_tighten_a_link_later(self):
        share = await self.make()
        path = f"/api/shares/{share['id']}"
        r = await self.a.patch(path, {"access": 1, "max_downloads": 1, "password": "later pass"})
        self.assertEqual((r.status, r.json()["share"]["access"], r.json()["share"]["max_downloads"], r.json()["share"]["has_password"]),
                         (200, 1, 1, True))
        self.assertTrue((await self.a.patch(path, {"clear_password": True})).json()["share"]["has_password"] is False)
        self.assertEqual((await self.a.patch(path, {"hours": 5})).status, 200)
        self.assertEqual((await self.b.patch(path, {"access": 0})).status, 404)             # not bob's
        self.assertEqual((await self.a.patch("/api/shares/9999", {"access": 0})).status, 404)
        for bad in ({"access": 7}, {"max_downloads": -1}, {"hours": 0}, {"password": "ab"}):
            self.assertEqual((await self.a.patch(path, bad)).status, 400, bad)
        self.assertEqual((await self.a.patch(path, {"access": 1}, csrf=False)).status, 403)
        await self.a.put("/api/admin/sharing", {"min_access": 1})
        self.assertEqual((await self.a.patch(path, {"access": 0})).json()["share"]["access"], 1)   # can't loosen below the minimum
        self.assertEqual((await self.a.patch(path, {"owner": 99, "token": "x", "name": "z"})).json()["share"]["name"], "Private.mp4")

    async def test_posting_to_the_landing_page_is_refused(self):
        share = await self.make()
        self.assertEqual((await Client(self.app).post(share["path"], body=b"x")).status, 405)

    async def test_old_databases_get_the_new_columns(self):
        import sqlite3
        conn = sqlite3.connect(shares.DB_PATH)
        conn.execute("DROP TABLE IF EXISTS web_shares")
        conn.execute("CREATE TABLE web_shares (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, owner INTEGER NOT NULL, "
                     "name TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, "
                     "downloads INTEGER NOT NULL DEFAULT 0, max_downloads INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO web_shares (token, owner, name, size, created, expires) VALUES ('t', 1, 'n', 1, 0, 9999999999)")
        conn.commit()
        conn.close()
        old = shares.get(1)
        self.assertEqual((old.access, old.pw_hash), (0, ""))


class BotOptions(BotButtons):
    async def test_the_buttons_under_a_new_link_tighten_it(self):
        path = self.tmp / "tmp" / "v.mp4"
        path.write_bytes(b"v")
        context, _ = await self.run_cb([{"path": path, "name": "v.mp4"}])
        markup = context.bot.send_message.await_args.kwargs["reply_markup"]
        data = [b.callback_data for b in markup.inline_keyboard[0]]
        share = shares.list_for(42)[0]
        self.assertEqual(data, [f"ua|{share.id}|login", f"ua|{share.id}|once", f"ul|{share.id}"])
        for cb in data[:2]:
            update, ctx, query = self.update(user_id=42, data=cb)
            with mock.patch.object(main, "gate_callback", mock.AsyncMock(return_value=True)):
                await main.link_options_callback(update, ctx)
        share = shares.list_for(42)[0]
        self.assertEqual((share.access, share.max_downloads), (shares.LOGIN, 1))
        self.assertIn("🔒", main._links_view(42)[0])
        self.assertIn("1️⃣", main._links_view(42)[0])
        shares.update(share.id, 42, {"password": "pw12345"})
        self.assertIn("🔑", main._links_view(42)[0])

    async def test_someone_else_cannot_use_the_buttons_and_junk_is_ignored(self):
        path = self.tmp / "tmp" / "v.mp4"
        path.write_bytes(b"v")
        share = shares.create(42, path, "v.mp4")
        for data, user in ((f"ua|{share.id}|login", 43), (f"ua|{share.id}|bogus", 42), ("ua|x|login", 42), ("ua|1", 42)):
            update, ctx, query = self.update(user_id=user, data=data)
            with mock.patch.object(main, "gate_callback", mock.AsyncMock(return_value=True)):
                await main.link_options_callback(update, ctx)
        self.assertEqual(shares.get(share.id).access, shares.ANYONE)
        self.assertEqual(shares.get(share.id).max_downloads, 0)
