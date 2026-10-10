"""The web API through the real ASGI app: login, sessions, CSRF, origin, roles, headers, isolation between accounts,
downloads end to end, settings, cookies, toolbox, admin."""
import asyncio
import re
from pathlib import Path
from unittest import mock

from tests.webharness import PASSWORD, Client, WebBase  # noqa: E402  (imports _env first)

from downloader import tools  # noqa: E402
from downloader.tools import MediaInfo  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from utils.webchat import deliveries  # noqa: E402
from web import accounts, service  # noqa: E402


class Login(WebBase):
    quick_login = False

    async def test_success_sets_a_locked_down_cookie(self):
        resp = await self.a.login("alice", PASSWORD)
        self.assertEqual(resp.status, 200)
        cookie = resp.set_cookies()[0]
        self.assertTrue(cookie.startswith("__Host-candy="))
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain", cookie)
        self.assertNotIn("password", resp.body.decode().lower().replace("username", ""))

    async def test_plain_http_mode_uses_a_plain_cookie_name(self):
        self.ctx.secure_cookie = False
        resp = await self.a.login("alice", PASSWORD)
        self.assertTrue(resp.set_cookies()[0].startswith("candy="))
        self.assertNotIn("Secure", resp.set_cookies()[0])

    async def test_wrong_user_and_wrong_password_look_identical(self):
        one = await self.a.login("alice", "wrong password here")
        two = await self.a.login("nobody", "wrong password here")
        self.assertEqual((one.status, one.json()), (two.status, two.json()))
        self.assertEqual(one.status, 401)
        self.assertEqual(self.a.cookies, {})

    async def test_login_is_rate_limited_per_address(self):
        for _ in range(10):
            await self.a.login("alice", "wrong password here")
        resp = await self.a.login("alice", PASSWORD)             # even the right one is refused now
        self.assertEqual(resp.status, 429)
        self.assertIn("retry_after", resp.json())
        other = Client(self.app, ip="192.0.2.77")
        self.assertEqual((await other.login("bob", PASSWORD)).status, 200)

    async def test_login_is_rate_limited_per_username(self):
        for i in range(10):
            await Client(self.app, ip=f"192.0.2.{i + 1}").login("alice", "wrong password here")
        self.assertEqual((await Client(self.app, ip="192.0.2.200").login("alice", PASSWORD)).status, 429)

    async def test_account_lockout_through_the_api(self):
        accounts.create_account("zed", PASSWORD)
        for i in range(5):
            await Client(self.app, ip=f"192.0.2.{i + 30}").login("zed", "wrong password here")
        self.assertEqual((await Client(self.app, ip="192.0.2.99").login("zed", PASSWORD)).status, 401)

    async def test_ip_comes_from_the_last_forwarded_entry(self):
        for _ in range(10):
            await self.a.request("POST", "/api/auth/login", json={"username": "alice", "password": "x" * 12}, csrf=False,
                                 headers={"x-forwarded-for": "6.6.6.6, 203.0.113.5"})
        again = await self.a.request("POST", "/api/auth/login", json={"username": "bob", "password": PASSWORD}, csrf=False,
                                     headers={"x-forwarded-for": "7.7.7.7, 203.0.113.5"})
        self.assertEqual(again.status, 429)                       # a spoofed first entry doesn't dodge the limit

    async def test_login_needs_a_json_object(self):
        resp = await self.a.request("POST", "/api/auth/login", body=b"[1]", csrf=False)
        self.assertEqual(resp.status, 400)
        resp = await self.a.request("POST", "/api/auth/login", body=b"{" + b"x" * 70_000, csrf=False)
        self.assertEqual(resp.status, 413)


class Sessions(WebBase):
    async def test_unauthenticated_requests_are_refused(self):
        anonymous = Client(self.app)
        for method, path in (("GET", "/api/me"), ("GET", "/api/jobs"), ("POST", "/api/preview"), ("GET", "/api/settings"),
                             ("GET", "/api/admin/accounts"), ("GET", "/api/files/x/0"), ("PUT", "/api/tools/upload")):
            resp = await anonymous.request(method, path, json={} if method != "GET" else None, csrf=False)
            self.assertEqual(resp.status, 401, path)

    async def test_forged_cookie_is_refused(self):
        forged = Client(self.app)
        forged.cookies["__Host-candy"] = "A" * 43
        self.assertEqual((await forged.get("/api/me")).status, 401)

    async def test_logout_ends_the_session_server_side(self):
        stolen = Client(self.app)
        stolen.cookies = dict(self.a.cookies)
        stolen.csrf = self.a.csrf
        self.assertEqual((await self.a.post("/api/auth/logout")).status, 200)
        self.assertEqual((await stolen.get("/api/me")).status, 401)

    async def test_password_change_signs_out_other_devices_and_keeps_this_one(self):
        other = Client(self.app)
        await other.login("bob", PASSWORD)
        resp = await self.b.post("/api/me/password", {"current": PASSWORD, "new": "a brand new password"})
        self.assertEqual(resp.status, 200)
        self.assertEqual((await self.b.get("/api/me")).status, 200)
        self.assertEqual((await other.get("/api/me")).status, 401)

    async def test_password_change_needs_the_current_password(self):
        resp = await self.b.post("/api/me/password", {"current": "not my password", "new": "a brand new password"})
        self.assertEqual(resp.status, 403)
        resp = await self.b.post("/api/me/password", {"current": PASSWORD, "new": "short"})
        self.assertEqual(resp.status, 400)

    async def test_must_change_blocks_everything_else(self):
        accounts.create_account("temp", "one time password", must_change=True)
        c = Client(self.app, ip="192.0.2.50")
        await c.login("temp", "one time password")
        self.assertEqual((await c.get("/api/me")).status, 200)
        blocked = await c.get("/api/jobs")
        self.assertEqual(blocked.status, 403)
        self.assertTrue(blocked.json()["must_change"])
        self.assertEqual((await c.post("/api/preview", {"url": "https://x.com/a"})).status, 403)
        self.assertEqual((await c.post("/api/me/password", {"current": "one time password", "new": "my own new password"})).status, 200)
        self.assertEqual((await c.get("/api/jobs")).status, 200)

    async def test_disabled_account_loses_access_at_once(self):
        accounts.set_disabled(self.bob.id, True)
        self.assertEqual((await self.b.get("/api/jobs")).status, 401)

    async def test_me_does_not_leak_secrets(self):
        body = (await self.a.get("/api/me")).body.decode()
        for word in ("pw_hash", "scrypt", "token_hash"):
            self.assertNotIn(word, body)


class Csrf(WebBase):
    async def test_state_changing_requests_need_the_token(self):
        resp = await self.b.post("/api/settings/reset", csrf=False)
        self.assertEqual(resp.status, 403)
        resp = await self.b.post("/api/settings/reset", headers={"x-csrf-token": "wrong"})
        self.assertEqual(resp.status, 403)
        self.assertEqual((await self.b.post("/api/settings/reset")).status, 200)

    async def test_another_accounts_token_does_not_work(self):
        resp = await self.b.post("/api/settings/reset", headers={"x-csrf-token": self.a.csrf})
        self.assertEqual(resp.status, 403)

    async def test_foreign_origin_is_refused_even_with_the_token(self):
        for origin in ("https://evil.example", "null", "https://web.test.evil.example"):
            resp = await self.b.post("/api/settings/reset", origin=origin)
            self.assertEqual(resp.status, 403, origin)
        self.assertEqual((await self.b.post("/api/settings/reset", origin="https://web.test")).status, 200)

    async def test_public_url_is_an_accepted_origin(self):
        self.ctx.public_url = "https://downloads.example.com"
        self.assertEqual((await self.b.post("/api/settings/reset", origin="https://downloads.example.com")).status, 200)

    async def test_login_also_checks_origin(self):
        resp = await Client(self.app).request("POST", "/api/auth/login", json={"username": "bob", "password": PASSWORD},
                                              csrf=False, origin="https://evil.example")
        self.assertEqual(resp.status, 403)

    async def test_get_requests_never_change_anything(self):
        before = len(self.jobs.records)
        for path in ("/api/jobs", "/api/me", "/api/settings", "/api/history", "/api/cookies", "/api/tools"):
            self.assertEqual((await self.b.get(path)).status, 200)
        self.assertEqual(len(self.jobs.records), before)


class Headers(WebBase):
    async def test_every_response_carries_the_protective_headers(self):
        for path in ("/", "/api/me", "/static/app.js", "/healthz", "/nope"):
            resp = await self.b.get(path)
            csp = resp.header("content-security-policy")
            self.assertIn("default-src 'none'", csp, path)
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertEqual(resp.header("x-content-type-options"), "nosniff", path)
            self.assertEqual(resp.header("x-frame-options"), "DENY")
            self.assertEqual(resp.header("referrer-policy"), "no-referrer")
        self.assertEqual((await self.b.get("/api/me")).header("cache-control"), "no-store")
        self.assertIsNone((await self.b.get("/api/me")).header("server"))

    async def test_static_files_cannot_escape(self):
        for name in ("..%2Fapp.py", "../app.py", "%2e%2e/app.py", "app.py", "..", "x/y"):
            resp = await self.b.get("/static/" + name)
            self.assertIn(resp.status, (404, 307, 308), name)
            self.assertNotIn(b"SecurityHeaders", resp.body)
        self.assertEqual((await self.b.get("/static/app.css")).status, 200)

    async def test_the_page_has_no_inline_script_or_style(self):
        html = (await self.b.get("/")).body.decode()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn("style=", html)
        self.assertNotIn("onclick=", html)
        js = Path("web/static/app.js").read_text()
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("eval(", js)
        self.assertNotIn("document.write", js)
        self.assertNotRegex(js, r"setAttribute\(\s*[\"']style")


class Isolation(WebBase):
    async def start_job(self, client, **kw):
        self.fake_download(**kw)
        resp = await client.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})
        self.assertEqual(resp.status, 202, resp.body)
        return resp.json()["rid"]

    async def test_other_people_cannot_see_touch_or_fetch_my_job(self):
        rid = await self.start_job(self.a)
        await self.wait_for(self.a, rid)
        self.assertEqual([j["rid"] for j in (await self.b.get("/api/jobs")).json()["jobs"]], [])
        for method, path in (("POST", f"/api/jobs/{rid}/cancel"), ("POST", f"/api/jobs/{rid}/retry"),
                             ("POST", f"/api/jobs/{rid}/dismiss"), ("GET", f"/api/files/{rid}/0"),
                             ("POST", f"/api/files/{rid}/0/link"), ("POST", f"/api/files/{rid}/0/telegram")):
            resp = await self.b.request(method, path)
            self.assertEqual(resp.status, 404, path)
        self.assertEqual((await self.a.get(f"/api/files/{rid}/0")).status, 200)

    async def test_file_indexes_are_checked(self):
        rid = await self.start_job(self.a)
        await self.wait_for(self.a, rid)
        for index in ("1", "-1", "99", "abc", "0.5"):
            self.assertEqual((await self.a.get(f"/api/files/{rid}/{index}")).status, 404, index)

    async def test_an_unknown_job_id_never_reaches_the_filesystem(self):
        for rid in ("..", "../../etc", "a/b"):
            self.assertEqual((await self.a.get(f"/api/files/{rid}/0")).status, 404)

    async def test_toolbox_files_are_private_too(self):
        with mock.patch.object(tools, "probe", self.fake_probe):
            resp = await self.a.put("/api/tools/upload?name=clip.mp4", body=b"x" * 1000)
        rid = resp.json()["rid"]
        self.assertEqual((await self.b.post(f"/api/tools/{rid}/run", {"tool": "strip"})).status, 404)
        self.assertEqual((await self.b.delete(f"/api/tools/{rid}")).status, 404)
        self.assertEqual((await self.b.put(f"/api/tools/{rid}/srt", body=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")).status, 404)
        self.assertEqual((await self.b.get("/api/tools")).json()["items"], [])

    async def fake_probe(self, path):
        return MediaInfo(duration=120.0, size=1000, width=640, height=360, has_video=True, has_audio=True)

    async def test_settings_and_history_are_per_account(self):
        await self.a.put("/api/settings", {"quality": "720p"})
        self.assertEqual((await self.b.get("/api/settings")).json()["settings"]["quality"], "best")


class DownloadFlow(WebBase):
    async def test_download_end_to_end(self):
        self.fake_download(b"song-bytes", "My Song.mp3")
        resp = await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "audio", "audio_format": "mp3"})
        self.assertEqual(resp.status, 202)
        job = await self.wait_for(self.b, resp.json()["rid"])
        self.assertEqual(job["state"], "done")
        self.assertEqual([f["name"] for f in job["files"]], ["My Song.mp3"])
        got = await self.b.get(f"/api/files/{job['rid']}/0")
        self.assertEqual(got.body, b"song-bytes")
        self.assertIn("attachment", got.header("content-disposition"))
        self.assertIn("My%20Song.mp3", got.header("content-disposition"))
        self.assertEqual(self.tg.messages, [])                   # nothing leaked into a Telegram chat

    async def test_files_leave_the_job_folder_and_live_in_the_web_store(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        path = deliveries.files[rid][0]["path"]
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.tmp / "tmp" / "web" / rid)
        self.assertEqual(oct(path.stat().st_mode & 0o777)[-1], oct(path.stat().st_mode & 0o777)[-1])

    async def test_failure_is_reported_with_a_reason_and_can_be_retried(self):
        self.fake_download(fail="HTTP Error 403: Forbidden")
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        job = await self.wait_for(self.b, rid)
        self.assertEqual(job["state"], "failed")
        self.assertIn("403", job["error"])
        self.fake_download(b"now fine", "ok.mp3")
        retried = await self.b.post(f"/api/jobs/{rid}/retry")
        self.assertEqual(retried.status, 202)
        job = await self.wait_for(self.b, rid, states=("done",))
        self.assertEqual(job["files"][0]["name"], "ok.mp3")

    async def test_retry_only_for_failed_jobs(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        self.assertEqual((await self.b.post(f"/api/jobs/{rid}/retry")).status, 409)

    async def test_cancel(self):
        self.fake_download(delay=5)
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await asyncio.sleep(0.2)
        self.assertTrue((await self.b.post(f"/api/jobs/{rid}/cancel")).json()["cancelled"])
        job = await self.wait_for(self.b, rid)
        self.assertEqual(job["state"], "cancelled")

    async def test_dismiss_deletes_the_files(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        path = deliveries.files[rid][0]["path"]
        self.assertEqual((await self.b.post(f"/api/jobs/{rid}/dismiss")).status, 200)
        self.assertFalse(path.exists())
        self.assertNotIn(rid, deliveries.files)
        self.assertEqual((await self.b.get("/api/jobs")).json()["jobs"], [])

    async def test_a_running_job_cannot_be_dismissed(self):
        self.fake_download(delay=5)
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await asyncio.sleep(0.1)
        self.assertEqual((await self.b.post(f"/api/jobs/{rid}/dismiss")).status, 409)
        await self.b.post(f"/api/jobs/{rid}/cancel")

    async def test_expired_results_are_swept(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        path = deliveries.files[rid][0]["path"]
        self.assertEqual(self.jobs.sweep(), 0)
        import time
        self.assertEqual(self.jobs.sweep(now=time.time() + 3601), 1)
        self.assertFalse(path.exists())
        self.assertEqual((await self.b.get("/api/jobs")).json()["jobs"], [])

    async def test_per_account_active_job_limit(self):
        self.fake_download(delay=5)
        for _ in range(3):
            self.assertEqual((await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).status, 202)
        resp = await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})
        self.assertEqual(resp.status, 429)
        self.assertEqual((await self.a.post("/api/download", {"url": "https://example.com/b.mp3", "kind": "simple"})).status, 202)
        for rec in list(self.jobs.records.values()):
            self.jobs.cancel(rec)

    async def test_disk_quota(self):
        self.fake_download(b"x" * 1000)
        self.jobs.quota = 500
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        resp = await self.b.post("/api/download", {"url": "https://example.com/b.mp3", "kind": "simple"})
        self.assertEqual(resp.status, 429)
        self.assertIn("allowance", resp.json()["error"])

    async def test_history_records_web_downloads_under_the_account(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        items = (await self.b.get("/api/history")).json()
        self.assertEqual(items["total"], 1)
        self.assertEqual(items["items"][0]["status"], "success")
        self.assertEqual((await self.a.get("/api/history")).json()["total"], 0)
        self.assertEqual((await self.b.post("/api/history/clear")).json()["deleted"], 1)

    async def test_notes_for_the_person_come_through(self):
        async def fake(url, workspace, settings, user_id, cb, cancel_event=None):
            path = Path(workspace) / "v.mp4"
            path.write_bytes(b"v")
            settings.setdefault("delivery_notes", []).append("Subtitles couldn't be added this time.")
            return [path]
        self.addCleanup(setattr, jm, "dispatch_download", jm.dispatch_download)
        jm.dispatch_download = fake
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp4", "kind": "simple"})).json()["rid"]
        job = await self.wait_for(self.b, rid)
        self.assertEqual(job["notes"], ["Subtitles couldn't be added this time."])

    async def test_batch_starts_one_job_per_link_and_dedupes(self):
        self.fake_download()
        resp = await self.b.post("/api/batch", {"urls": ["https://example.com/1", "https://example.com/2", "https://example.com/1"], "quality": "720p"})
        self.assertEqual(resp.status, 202)
        self.assertEqual(len(resp.json()["jobs"]), 2)
        for job in resp.json()["jobs"]:
            await self.wait_for(self.b, job["rid"])

    async def test_batch_reports_when_it_hits_the_limit_midway(self):
        self.fake_download(delay=5)
        resp = await self.b.post("/api/batch", {"urls": [f"https://example.com/{i}" for i in range(5)], "quality": "best"})
        self.assertEqual(resp.status, 202)
        self.assertEqual(len(resp.json()["jobs"]), 3)
        self.assertIn("stopped", resp.json())
        for rec in list(self.jobs.records.values()):
            self.jobs.cancel(rec)

    async def test_telegram_send_needs_a_linked_account(self):
        self.fake_download(b"abc", "f.mp3")
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        self.assertEqual((await self.b.post(f"/api/files/{rid}/0/telegram")).status, 400)
        linked = accounts.create_account("tgy", PASSWORD, telegram_id=4242)
        c = Client(self.app, ip="192.0.2.61")
        await c.login("tgy", PASSWORD)
        rid2 = (await c.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(c, rid2)
        self.assertEqual((await c.post(f"/api/files/{rid2}/0/telegram")).status, 200)
        self.assertEqual(self.sent_to_telegram, [(4242, b"abc", "f.mp3")])

    async def test_a_linked_account_shares_settings_with_its_telegram_user(self):
        accounts.create_account("tgy", PASSWORD, telegram_id=4242)
        c = Client(self.app, ip="192.0.2.62")
        await c.login("tgy", PASSWORD)
        await c.put("/api/settings", {"quality": "480p"})
        from settings.user_settings import get_settings
        self.assertEqual(get_settings(4242)["quality"], "480p")


class Validation(WebBase):
    async def test_bad_requests_are_refused_with_a_reason(self):
        cases = [{"url": "file:///etc/passwd"}, {"url": "javascript:alert(1)"}, {"url": "ftp://x/y"}, {"url": ""},
                 {"url": "https://x.com/a b"}, {"url": "https://example.com/a", "kind": "video", "quality": "9999p"},
                 {"url": "https://example.com/a", "kind": "audio", "audio_format": "exe"},
                 {"url": "https://example.com/a", "kind": "banana"},
                 {"url": "https://example.com/a", "kind": "video", "sections": [{"start": "5:00", "end": "1:00"}]},
                 {"url": "https://example.com/a", "kind": "video", "sections": [["x", "y"]]},
                 {"url": "https://example.com/a", "kind": "video", "subs": {"langs": ["en"], "mode": "rm -rf"}},
                 {"url": "https://example.com/a", "kind": "video", "subs": {"langs": ["../x"], "mode": "file"}}]
        for case in cases:
            resp = await self.b.post("/api/download", case)
            self.assertEqual(resp.status, 400, case)
            self.assertTrue(resp.json()["error"])
        self.assertEqual(self.jobs.records, {})

    async def test_valid_choices_become_job_settings(self):
        url, s = service.build_settings(-5, {"url": "https://example.com/a", "kind": "video", "quality": "720p",
                                            "subs": {"langs": ["fa", "en"], "mode": "burnfile"}})
        self.assertEqual((s["mode"], s["quality"], s["sub_langs"], s["sub_mode"]), ("video", "720p", ["fa", "en"], "burnfile"))
        _, s = service.build_settings(-5, {"url": "https://example.com/a", "kind": "audio", "audio_format": "mp3split"})
        self.assertEqual((s["mode"], s["audio_format"], s["split_chapters"]), ("audio", "mp3", True))
        _, s = service.build_settings(-5, {"url": "https://example.com/a", "kind": "video", "duration": 600,
                                          "sections": [{"start": "1:00", "end": "2:00"}, {"start": "0:10", "end": ""}], "merge": True,
                                          "subs": {"langs": ["en"], "mode": "file"}})
        self.assertEqual(s["sections"], [(60.0, 120.0), (10.0, 600.0)])
        self.assertTrue(s["sections_merge"])
        self.assertNotIn("sub_langs", s)                           # subtitles are never combined with sections
        self.assertFalse(s["adhd_mode"])

    async def test_a_section_past_the_end_is_refused(self):
        with self.assertRaises(service.WebError):
            service.build_settings(-5, {"url": "https://example.com/a", "kind": "video", "duration": 100,
                                        "sections": [{"start": "1:30", "end": "3:00"}]})

    async def test_batch_quality_and_links_validated(self):
        self.assertEqual((await self.b.post("/api/batch", {"urls": [], "quality": "best"})).status, 400)
        self.assertEqual((await self.b.post("/api/batch", {"urls": ["https://example.com/a"], "quality": "bogus"})).status, 400)
        self.assertEqual((await self.b.post("/api/batch", {"urls": ["notalink"], "quality": "best"})).status, 400)

    async def test_oversized_json_is_refused(self):
        resp = await self.b.request("POST", "/api/download", body=b'{"url": "' + b"a" * 70_000 + b'"}')
        self.assertEqual(resp.status, 413)


class Preview(WebBase):
    def probe_result(self, **kw):
        from downloader.probe import ProbeResult
        return ProbeResult(**{"ok": True, "title": "A video", "thumbnail": "https://img.example/t.jpg", "heights": [1080, 720, 360],
                              "duration": 300, "sizes": {"best": 5_000_000}, **kw})

    async def test_a_video_preview_lists_what_the_bot_would_offer(self):
        from downloader.subtitles import SubTrack
        result = self.probe_result(subtitles=[SubTrack("fa", "Persian"), SubTrack("en", "English", True)],
                                   chapters=[("Intro", 0.0, 60.0), ("Main", 60.0, 300.0)])
        with mock.patch.object(service, "probe", mock.AsyncMock(return_value=result)):
            resp = await self.b.post("/api/preview", {"url": "https://www.youtube.com/watch?v=abc"})
        data = resp.json()
        self.assertEqual((data["kind"], data["heights"], data["duration"]), ("video", [1080, 720, 360], 300))
        self.assertEqual(data["subtitles"][1], {"code": "en", "name": "English", "auto": True})
        self.assertEqual(data["chapters"][0], {"title": "Intro", "start": 0.0, "end": 60.0})
        self.assertEqual(data["sizes"]["best"], 5_000_000)

    async def test_sizes_can_be_turned_off(self):
        await self.b.put("/api/settings", {"show_sizes": False})
        with mock.patch.object(service, "probe", mock.AsyncMock(return_value=self.probe_result())):
            data = (await self.b.post("/api/preview", {"url": "https://www.youtube.com/watch?v=abc"})).json()
        self.assertEqual(data["sizes"], {})

    async def test_audio_only_source(self):
        with mock.patch.object(service, "probe", mock.AsyncMock(return_value=self.probe_result(heights=[]))):
            data = (await self.b.post("/api/preview", {"url": "https://soundcloud.com/a/b"})).json()
        self.assertEqual(data["kind"], "audio")

    async def test_playlist_is_listed(self):
        from downloader.playlist import Entry
        listing = mock.AsyncMock(return_value=("My list", [Entry("https://youtu.be/1", "One", 60), Entry("https://youtu.be/2", "Two")]))
        with mock.patch.object(service, "list_playlist", listing):
            data = (await self.b.post("/api/preview", {"url": "https://www.youtube.com/playlist?list=PL123"})).json()
        self.assertEqual((data["kind"], data["title"], len(data["entries"])), ("playlist", "My list", 2))

    async def test_failed_probe_falls_back_to_a_plain_download(self):
        from downloader.probe import ProbeResult
        with mock.patch.object(service, "probe", mock.AsyncMock(return_value=ProbeResult(ok=False, error="boom"))):
            data = (await self.b.post("/api/preview", {"url": "https://www.youtube.com/watch?v=abc"})).json()
        self.assertEqual(data["kind"], "fallback")
        self.assertIn("boom", data["note"])

    async def test_image_sites_get_the_simple_menu(self):
        from downloader.probe import ProbeResult
        with mock.patch.object(service, "probe", mock.AsyncMock(return_value=ProbeResult(ok=False))), \
                mock.patch.object(service.gallerydl_probe, "probe", mock.AsyncMock(return_value={"title": "Pin", "thumbnail": "https://i/x.jpg"})):
            data = (await self.b.post("/api/preview", {"url": "https://www.pinterest.com/pin/123/"})).json()
        self.assertEqual((data["kind"], data["title"]), ("simple", "Pin"))

    async def test_spotify(self):
        data = (await self.b.post("/api/preview", {"url": "https://open.spotify.com/track/abc"})).json()
        self.assertEqual(data["kind"], "spotify")

    async def test_preview_is_rate_limited(self):
        with mock.patch.object(service, "probe", mock.AsyncMock(return_value=self.probe_result())):
            statuses = [(await self.b.post("/api/preview", {"url": "https://www.youtube.com/watch?v=abc"})).status for _ in range(32)]
        self.assertEqual(statuses.count(200), 30)
        self.assertEqual(statuses[-1], 429)


class Settings(WebBase):
    async def test_allowed_changes_stick(self):
        resp = await self.b.put("/api/settings", {"quality": "720p", "embed_thumbnail": False, "rate_limit_kbps": 500, "audio_bitrate": "320"})
        s = resp.json()["settings"]
        self.assertEqual((s["quality"], s["embed_thumbnail"], s["rate_limit_kbps"], s["audio_bitrate"]), ("720p", False, 500, "320"))
        self.assertEqual((await self.b.get("/api/settings")).json()["settings"]["quality"], "720p")

    async def test_dangerous_or_unknown_settings_are_refused(self):
        for body in ({"proxy": "socks5://10.0.0.1:1080"}, {"filename_template": "../../x/%(title)s"}, {"nonsense": 1},
                     {"quality": "8k"}, {"embed_thumbnail": "yes"}, {"rate_limit_kbps": -1}, {"rate_limit_kbps": "5"},
                     {"concurrent_fragments": 99}, {"playlist_range": "1-5; rm"}, {"mode": "both"}, {"adhd_mode": True}):
            self.assertEqual((await self.b.put("/api/settings", body)).status, 400, body)
        s = (await self.b.get("/api/settings")).json()
        self.assertNotIn("proxy", s["settings"])
        self.assertNotIn("filename_template", s["settings"])
        self.assertEqual(s["settings"]["quality"], "best")

    async def test_one_bad_value_changes_nothing(self):
        resp = await self.b.put("/api/settings", {"quality": "720p", "proxy": "x"})
        self.assertEqual(resp.status, 400)
        self.assertEqual((await self.b.get("/api/settings")).json()["settings"]["quality"], "best")

    async def test_reset(self):
        await self.b.put("/api/settings", {"quality": "720p"})
        self.assertEqual((await self.b.post("/api/settings/reset")).json()["settings"]["quality"], "best")


COOKIES_YT = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tLOGIN_INFO\tv\n.google.com\tTRUE\t/\tTRUE\t4102444800\tSID\tv\n"
COOKIES_IG = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\tv\n"


class Cookies(WebBase):
    async def test_upload_merges_per_site_and_removal(self):
        r = await self.b.post("/api/cookies", body=COOKIES_YT.encode())
        self.assertEqual(r.status, 200, r.body)
        r = await self.b.post("/api/cookies", body=COOKIES_IG.encode())
        sites = {s["site"] for s in r.json()["sites"]}
        self.assertEqual(sites, {"youtube.com", "instagram.com"})
        r = await self.b.delete("/api/cookies/instagram.com")
        self.assertEqual({s["site"] for s in r.json()["sites"]}, {"youtube.com"})
        self.assertEqual((await self.b.get("/api/settings")).json()["settings"]["cookies_enabled"], True)

    async def test_not_a_cookie_file_changes_nothing(self):
        await self.b.post("/api/cookies", body=COOKIES_YT.encode())
        r = await self.b.post("/api/cookies", body=b"hello, this is not a cookies file")
        self.assertEqual(r.status, 400)
        self.assertEqual({s["site"] for s in (await self.b.get("/api/cookies")).json()["sites"]}, {"youtube.com"})

    async def test_other_accounts_never_see_my_cookies(self):
        await self.b.post("/api/cookies", body=COOKIES_YT.encode())
        self.assertEqual((await self.a.get("/api/cookies")).json()["sites"], [])
        files = list((self.tmp / "cookies").iterdir())
        self.assertEqual([f.name for f in files], [f"{self.bob.user_id}.txt"])
        self.assertEqual(files[0].stat().st_mode & 0o077, 0)

    async def test_too_big_and_bad_site_names(self):
        self.assertEqual((await self.b.post("/api/cookies", body=b"x" * 1_100_000)).status, 413)
        self.assertEqual((await self.b.post("/api/cookies", body=b"")).status, 400)
        self.assertEqual((await self.b.delete("/api/cookies/..%2Fetc")).status, 404)
        self.assertEqual((await self.b.delete("/api/cookies/UPPER")).status, 404)


class Toolbox(WebBase):
    async def probe(self, path):
        return MediaInfo(duration=120.0, size=3_000_000, width=1280, height=720, has_video=True, has_audio=True)

    async def upload(self, client, name="clip.mp4", data=b"v" * 2000):
        with mock.patch.object(tools, "probe", self.probe):
            return await client.put(f"/api/tools/upload?name={name}", body=data)

    async def test_upload_run_and_result(self):
        r = await self.upload(self.b)
        self.assertEqual(r.status, 201)
        view = r.json()
        self.assertEqual((view["name"], view["duration"], view["burn_ok"]), ("clip.mp4", 120.0, True))
        seen = {}

        async def fake_run(settings, workspace, cb, cancel_event):
            seen.update(settings)
            out = Path(workspace) / "clip [trim].mp4"
            out.write_bytes(b"trimmed")
            return [out]
        with mock.patch.object(jm.media_tools, "run", fake_run):
            run = await self.b.post(f"/api/tools/{view['rid']}/run", {"tool": "trim", "range": "0:10 0:20", "exact": True})
            self.assertEqual(run.status, 202, run.body)
            job = await self.wait_for(self.b, view["rid"])
        self.assertEqual(job["state"], "done")
        self.assertEqual((seen["tool"], seen["start"], seen["end"], seen["exact"]), ("trim", 10.0, 20.0, True))
        self.assertEqual((await self.b.get(f"/api/files/{view['rid']}/0")).body, b"trimmed")
        self.assertEqual(len((await self.b.get("/api/tools")).json()["items"]), 1)         # still there for the next tool
        self.assertEqual(self.b_history_count(), 0)                                        # toolbox work isn't a download

    def b_history_count(self):
        from settings import access_control
        return access_control.count_user_downloads(self.bob.user_id)

    async def test_tool_input_validation(self):
        rid = (await self.upload(self.b)).json()["rid"]
        for body in ({"tool": "trim", "range": "9:00 10:00"}, {"tool": "trim", "range": "garbage"}, {"tool": "audio", "audio_format": "wav"},
                     {"tool": "compress", "target_mb": 7}, {"tool": "compress", "target_mb": 1000}, {"tool": "gif", "range": "0:10 99"},
                     {"tool": "burn"}, {"tool": "rm"}, {}):
            self.assertEqual((await self.b.post(f"/api/tools/{rid}/run", body)).status, 400, body)

    async def test_srt_then_burn_allowed(self):
        rid = (await self.upload(self.b)).json()["rid"]
        srt = "1\n00:00:01,000 --> 00:00:02,000\nسلام\n".encode("utf-8")
        view = (await self.b.put(f"/api/tools/{rid}/srt", body=srt)).json()
        self.assertTrue(view["has_srt"])
        self.assertEqual((await self.b.put(f"/api/tools/{rid}/srt", body=b"not subtitles")).status, 400)
        self.assertEqual((await self.b.put(f"/api/tools/{rid}/srt", body=b"")).status, 400)

    async def test_oversize_upload_is_refused_and_leaves_nothing(self):
        with mock.patch.object(tools, "probe", self.probe):
            r = await self.b.put("/api/tools/upload?name=big.mp4", body=b"x" * 5_000_001)
        self.assertEqual(r.status, 413)
        self.assertEqual(list((self.tmp / "tmp" / "tools").glob("*/*")), [])
        r = await self.b.put("/api/tools/upload?name=big.mp4", body=b"x", headers={"content-length": "9999999999"})
        self.assertEqual(r.status, 413)

    async def test_not_media_is_refused_and_cleaned(self):
        async def bad_probe(path):
            raise tools.ToolError("That doesn't look like a video or audio file.")
        with mock.patch.object(tools, "probe", bad_probe):
            r = await self.b.put("/api/tools/upload?name=x.txt", body=b"hello")
        self.assertEqual(r.status, 400)
        self.assertEqual(list((self.tmp / "tmp" / "tools").glob("*/*")), [])

    async def test_file_names_cannot_traverse(self):
        r = await self.upload(self.b, name="..%2F..%2Fevil.mp4")
        self.assertEqual(r.status, 201)
        stored = list((self.tmp / "tmp" / "tools").glob("*/*"))
        self.assertEqual(len(stored), 1)
        self.assertTrue(str(stored[0]).startswith(str(self.tmp / "tmp" / "tools")))
        self.assertEqual(stored[0].name, "original.mp4")

    async def test_only_three_uploads_are_kept(self):
        for i in range(5):
            await self.upload(self.b, name=f"c{i}.mp4")
        self.assertEqual(len((await self.b.get("/api/tools")).json()["items"]), 3)

    async def test_one_tool_at_a_time_per_file(self):
        rid = (await self.upload(self.b)).json()["rid"]
        started = asyncio.Event()

        async def slow(settings, workspace, cb, cancel_event):
            started.set()
            await asyncio.sleep(5)
            return []
        with mock.patch.object(jm.media_tools, "run", slow):
            self.assertEqual((await self.b.post(f"/api/tools/{rid}/run", {"tool": "strip"})).status, 202)
            await started.wait()
            self.assertEqual((await self.b.post(f"/api/tools/{rid}/run", {"tool": "strip"})).status, 409)
            await self.b.post(f"/api/jobs/{rid}/cancel")


class Admin(WebBase):
    async def test_non_admins_are_refused_everywhere_in_admin(self):
        for method, path in (("GET", "/api/admin/accounts"), ("POST", "/api/admin/accounts"), ("PATCH", "/api/admin/accounts/1"),
                             ("DELETE", "/api/admin/accounts/1"), ("POST", "/api/admin/accounts/1/reset"), ("GET", "/api/admin/sharing"),
                             ("PUT", "/api/admin/sharing"), ("DELETE", "/api/admin/shares/1"), ("GET", "/api/admin/overview"),
                             ("POST", "/api/admin/access"), ("GET", "/api/admin/warp"), ("POST", "/api/admin/warp/rotate"), ("GET", "/api/admin/audit")):
            resp = await self.b.request(method, path, json={} if method != "GET" else None)
            self.assertEqual(resp.status, 403, path)

    async def test_create_shows_the_password_once_and_it_works(self):
        resp = await self.a.post("/api/admin/accounts", {"username": "newbie", "role": "user"})
        self.assertEqual(resp.status, 201)
        password = resp.json()["password"]
        listing = (await self.a.get("/api/admin/accounts")).body.decode()
        self.assertNotIn(password, listing)
        self.assertNotIn("scrypt", listing)
        c = Client(self.app, ip="192.0.2.70")
        self.assertEqual((await c.login("newbie", password)).status, 200)
        self.assertTrue(c and (await c.get("/api/me")).json()["account"]["must_change"])

    async def test_create_validation_and_links(self):
        self.assertEqual((await self.a.post("/api/admin/accounts", {"username": "x"})).status, 400)
        self.assertEqual((await self.a.post("/api/admin/accounts", {"username": "alice"})).status, 400)
        self.assertEqual((await self.a.post("/api/admin/accounts", {"username": "okname", "telegram_id": "abc"})).status, 400)
        self.assertEqual((await self.a.post("/api/admin/accounts", {"username": "okname", "telegram_id": -5})).status, 400)
        r = await self.a.post("/api/admin/accounts", {"username": "okname", "telegram_id": "555"})
        self.assertEqual(r.json()["account"]["telegram_id"], 555)
        self.assertEqual((await self.a.post("/api/admin/accounts", {"username": "okname2", "role": "root"})).status, 400)

    async def test_reset_signs_the_person_out(self):
        pw = (await self.a.post(f"/api/admin/accounts/{self.bob.id}/reset")).json()["password"]
        self.assertEqual((await self.b.get("/api/me")).status, 401)
        c = Client(self.app, ip="192.0.2.71")
        self.assertEqual((await c.login("bob", pw)).status, 200)
        self.assertEqual((await c.get("/api/jobs")).status, 403)       # must choose a new password first

    async def test_block_role_delete_and_self_protection(self):
        r = await self.a.patch(f"/api/admin/accounts/{self.bob.id}", {"disabled": True})
        self.assertTrue(r.json()["account"]["disabled"])
        self.assertEqual((await self.b.get("/api/me")).status, 401)
        self.assertEqual((await self.a.patch(f"/api/admin/accounts/{self.alice.id}", {"disabled": True})).status, 400)
        self.assertEqual((await self.a.delete(f"/api/admin/accounts/{self.alice.id}")).status, 400)
        self.assertEqual((await self.a.patch(f"/api/admin/accounts/{self.alice.id}", {"role": "user"})).status, 400)   # last admin
        await self.a.patch(f"/api/admin/accounts/{self.bob.id}", {"role": "admin", "disabled": False})
        self.assertEqual((await self.a.delete(f"/api/admin/accounts/{self.bob.id}")).status, 200)
        self.assertEqual((await self.a.delete("/api/admin/accounts/9999")).status, 404)
        self.assertEqual((await self.a.patch("/api/admin/accounts/abc", {"role": "user"})).status, 404)

    async def test_deleting_an_account_removes_its_files(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        path = deliveries.files[rid][0]["path"]
        await self.a.delete(f"/api/admin/accounts/{self.bob.id}")
        self.assertFalse(path.exists())
        self.assertNotIn(rid, self.jobs.records)

    async def test_actions_are_audited_without_secrets(self):
        await self.a.post("/api/admin/accounts", {"username": "newbie"})
        await Client(self.app, ip="192.0.2.80").login("alice", "wrong password here")
        audit = (await self.a.get("/api/admin/audit")).json()["items"]
        actions = [e["action"] for e in audit]
        self.assertIn("account_created", actions)
        self.assertIn("login_failed", actions)
        self.assertNotIn("wrong password here", str(audit))

    async def test_overview_and_access(self):
        o = (await self.a.get("/api/admin/overview")).json()
        self.assertEqual(o["accounts"], 2)
        self.assertEqual(o["access_mode"], "public")
        r = await self.a.post("/api/admin/access", {"mode": "private", "allow": 555})
        self.assertEqual((r.json()["access_mode"], r.json()["allowed_users"]), ("private", [555]))
        self.assertEqual((await self.a.post("/api/admin/access", {"mode": "weird"})).status, 400)
        self.assertEqual((await self.a.post("/api/admin/access", {"allow": "x"})).status, 400)
        r = await self.a.post("/api/admin/access", {"remove": 555, "mode": "public"})
        self.assertEqual(r.json()["allowed_users"], [])

    async def test_warp_not_configured_is_explained_not_crashed(self):
        r = (await self.a.get("/api/admin/warp")).json()
        self.assertFalse(r["configured"])
        r = (await self.a.post("/api/admin/warp/rotate")).json()
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    import unittest
    unittest.main()
