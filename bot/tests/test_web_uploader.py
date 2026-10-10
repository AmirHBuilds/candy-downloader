"""The "Get a link" uploader: configuration safety (the key is encrypted and never shown), what is sent, how answers
are read, personal keys, and the buttons in the bot and in the web app."""
import asyncio
import sqlite3
from pathlib import Path
from unittest import mock

from tests.webharness import FakeTelegramBot, WebBase  # noqa: E402  (imports _env first)

import main  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from ui import quick_menu  # noqa: E402
from web import accounts, secrets_store, uploader  # noqa: E402

KEY = "sk-live-SUPERSECRET-123"
GOOD = {"url": "https://candyflix.example/api/upload", "enabled": True, "key": KEY, "response_path": "data.url"}


class Config(WebBase):
    quick_login = False

    def raw_db(self):
        conn = sqlite3.connect(uploader.DB_PATH)
        try:
            return str(conn.execute("SELECT * FROM uploader_config").fetchall())
        finally:
            conn.close()

    async def test_defaults_are_off(self):
        self.assertFalse(uploader.user_status(1)["available"])
        with self.assertRaises(uploader.UploaderError):
            await uploader.upload(self.tmp / "x", "x", 1)

    async def test_key_is_encrypted_at_rest_and_never_shown(self):
        view = uploader.set_global(GOOD)
        self.assertTrue(view["has_key"])
        self.assertNotIn("key", view)
        self.assertNotIn(KEY, str(uploader.public_global()))
        self.assertNotIn(KEY, self.raw_db())
        self.assertNotIn("candyflix.example", self.raw_db())                # the whole record is encrypted
        self.assertEqual(uploader.get_global()["key"], KEY)

    async def test_a_different_secret_cannot_read_it(self):
        uploader.set_global(GOOD)
        (self.tmp / "data" / "web_secret").unlink()
        secrets_store.reset()
        self.assertFalse(uploader.get_global().get("key"))
        self.assertFalse(uploader.user_status(1)["available"])

    async def test_secret_file_is_private(self):
        uploader.set_global(GOOD)
        mode = (self.tmp / "data" / "web_secret").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    async def test_blank_key_keeps_the_old_one_and_clear_removes_it(self):
        uploader.set_global(GOOD)
        uploader.set_global({"max_mb": 100, "key": ""})
        self.assertEqual(uploader.get_global()["key"], KEY)
        uploader.set_global({"clear_key": True})
        self.assertFalse(uploader.public_global()["has_key"])

    async def test_bad_configuration_is_refused(self):
        for bad in ({"url": "javascript:alert(1)"}, {"url": "file:///etc/passwd"}, {"url": "ftp://x/y"}, {"url": "https://u:p@host/x"},
                    {"url": "https:///nohost"}, {"auth_header": "Bad Header"}, {"auth_header": "X\r\nInjected: 1"},
                    {"file_field": "a b"}, {"max_mb": "lots"}, {"extra_fields": [1]}, {"extra_fields": {"a": 1}},
                    {"extra_fields": {str(i): "v" for i in range(11)}}, {"enabled": True}):
            with self.assertRaises(uploader.UploaderError, msg=str(bad)):
                uploader.set_global(bad)
        self.assertFalse(uploader.get_global()["enabled"])

    async def test_max_mb_is_clamped(self):
        uploader.set_global({"max_mb": 99999})
        self.assertEqual(uploader.get_global()["max_mb"], 2048)


class Sending(WebBase):
    quick_login = False

    def setUp(self):
        super().setUp()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.file = self.tmp / "clip.mp4"
        self.file.write_bytes(b"x" * 2_000_000)
        self.calls = []
        self.answer = (200, '{"data": {"url": "https://cdn.example/abc"}}')

        async def post(url, headers, fields, field, path, name):
            self.calls.append({"url": url, "headers": headers, "fields": fields, "field": field, "name": name})
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        self.addCleanup(setattr, uploader, "_post", uploader._post)
        uploader._post = post

    async def test_sends_what_was_configured(self):
        uploader.set_global({**GOOD, "file_field": "media", "extra_fields": {"folder": "candy"}})
        link = await uploader.upload(self.file, "clip.mp4", 5)
        self.assertEqual(link, "https://cdn.example/abc")
        call = self.calls[0]
        self.assertEqual(call["url"], GOOD["url"])
        self.assertEqual(call["headers"], {"Authorization": f"Bearer {KEY}"})
        self.assertEqual((call["field"], call["fields"], call["name"]), ("media", {"folder": "candy"}, "clip.mp4"))

    async def test_no_key_means_no_auth_header(self):
        uploader.set_global({"url": GOOD["url"], "enabled": True})
        await uploader.upload(self.file, "clip.mp4", 5)
        self.assertEqual(self.calls[0]["headers"], {})

    async def test_custom_header_and_prefix(self):
        uploader.set_global({**GOOD, "auth_header": "X-API-Key", "auth_prefix": ""})
        await uploader.upload(self.file, "c.mp4", 5)
        self.assertEqual(self.calls[0]["headers"], {"X-API-Key": KEY})

    async def test_too_big_is_refused_before_sending(self):
        uploader.set_global({**GOOD, "max_mb": 1})
        with self.assertRaises(uploader.UploaderError) as ctx:
            await uploader.upload(self.file, "c.mp4", 5)
        self.assertIn("1 MB", str(ctx.exception))
        self.assertEqual(self.calls, [])

    async def test_refused_key_and_server_errors_never_echo_the_key(self):
        uploader.set_global(GOOD)
        for status, words in ((401, "refused"), (403, "refused"), (500, "error")):
            self.answer = (status, f"denied for {KEY}")
            with self.assertRaises(uploader.UploaderError) as ctx:
                await uploader.upload(self.file, "c.mp4", 5)
            self.assertIn(words, str(ctx.exception))
            self.assertNotIn(KEY, str(ctx.exception))
        self.answer = ConnectionError(f"could not reach https://x using {KEY}")
        with self.assertRaises(uploader.UploaderError) as ctx:
            await uploader.upload(self.file, "c.mp4", 5)
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertNotIn("candyflix", str(ctx.exception))

    async def test_reading_the_link(self):
        self.assertEqual(uploader.extract_link('{"data": {"url": "https://a/b"}}', "data.url"), "https://a/b")
        self.assertEqual(uploader.extract_link('{"files": [{"link": "https://a/c"}]}', "files.0.link"), "https://a/c")
        self.assertEqual(uploader.extract_link('{"url": "https://a/d"}', ""), "https://a/d")
        self.assertEqual(uploader.extract_link("Done: https://a/e now", ""), "https://a/e")
        self.assertEqual(uploader.extract_link('{"wrong": 1} but see https://a/f', "data.url"), "https://a/f")
        for body, path in (('{"data": {"url": "javascript:alert(1)"}}', "data.url"), ("nothing here", ""), ("", ""), ("[]", "0.x")):
            with self.assertRaises(uploader.UploaderError):
                uploader.extract_link(body, path)

    async def test_personal_keys_only_when_allowed(self):
        uploader.set_global(GOOD)
        with self.assertRaises(uploader.UploaderError):
            uploader.set_user_key(7, "mine")
        uploader.set_global({"allow_user_keys": True})
        uploader.set_user_key(7, "my-own-key")
        await uploader.upload(self.file, "c.mp4", 7)
        await uploader.upload(self.file, "c.mp4", 8)
        self.assertEqual(self.calls[0]["headers"]["Authorization"], "Bearer my-own-key")
        self.assertEqual(self.calls[1]["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertNotIn("my-own-key", str(uploader.user_status(7)))
        uploader.set_user_key(7, "")
        self.assertFalse(uploader.user_status(7)["has_own_key"])

    async def test_turning_personal_keys_off_stops_using_them(self):
        uploader.set_global({**GOOD, "allow_user_keys": True})
        uploader.set_user_key(7, "my-own-key")
        uploader.set_global({"allow_user_keys": False})
        await uploader.upload(self.file, "c.mp4", 7)
        self.assertEqual(self.calls[0]["headers"]["Authorization"], f"Bearer {KEY}")


class WebEndpoints(WebBase):
    async def test_admin_configures_and_people_see_only_availability(self):
        r = await self.a.put("/api/admin/uploader", GOOD)
        self.assertEqual(r.status, 200, r.body)
        self.assertNotIn(KEY, r.body.decode())
        self.assertNotIn(KEY, (await self.a.get("/api/admin/uploader")).body.decode())
        status = (await self.b.get("/api/uploader")).json()
        self.assertTrue(status["available"])
        self.assertNotIn("candyflix", str((await self.b.get("/api/me")).body))
        self.assertTrue((await self.b.get("/api/me")).json()["uploader"]["available"])

    async def test_normal_users_cannot_configure(self):
        self.assertEqual((await self.b.put("/api/admin/uploader", GOOD)).status, 403)
        self.assertFalse(uploader.get_global()["enabled"])

    async def test_a_user_cannot_pick_the_address(self):
        await self.a.put("/api/admin/uploader", GOOD)
        resp = await self.b.put("/api/uploader/key", {"key": "mine", "url": "http://169.254.169.254/"})
        self.assertEqual(resp.status, 400)                                   # personal keys are off
        await self.a.put("/api/admin/uploader", {"allow_user_keys": True})
        self.assertEqual((await self.b.put("/api/uploader/key", {"key": "mine", "url": "http://169.254.169.254/"})).status, 200)
        self.assertEqual(uploader.get_global()["url"], GOOD["url"])

    async def test_file_link_end_to_end(self):
        await self.a.put("/api/admin/uploader", GOOD)
        sent = {}

        async def post(url, headers, fields, field, path, name):
            sent.update(url=url, content=Path(path).read_bytes(), name=name)
            return 200, '{"data": {"url": "https://cdn.example/zzz"}}'
        self.addCleanup(setattr, uploader, "_post", uploader._post)
        uploader._post = post
        self.fake_download(b"payload", "Track.mp3")
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        r = await self.b.post(f"/api/files/{rid}/0/link")
        self.assertEqual(r.json(), {"link": "https://cdn.example/zzz"})
        self.assertEqual((sent["content"], sent["name"]), (b"payload", "Track.mp3"))
        self.assertEqual((await self.a.post(f"/api/files/{rid}/0/link")).status, 404)         # not alice's file

    async def test_file_link_when_off_explains(self):
        self.fake_download()
        rid = (await self.b.post("/api/download", {"url": "https://example.com/a.mp3", "kind": "simple"})).json()["rid"]
        await self.wait_for(self.b, rid)
        r = await self.b.post(f"/api/files/{rid}/0/link")
        self.assertEqual(r.status, 400)
        self.assertIn("isn't set up", r.json()["error"])

    async def test_test_button(self):
        await self.a.put("/api/admin/uploader", GOOD)

        async def post(url, headers, fields, field, path, name):
            return 200, '{"data": {"url": "https://cdn.example/test"}}'
        self.addCleanup(setattr, uploader, "_post", uploader._post)
        uploader._post = post
        self.assertEqual((await self.a.post("/api/admin/uploader/test")).json(), {"link": "https://cdn.example/test"})

    async def test_bad_admin_input_is_a_clean_400(self):
        r = await self.a.put("/api/admin/uploader", {"url": "javascript:alert(1)"})
        self.assertEqual(r.status, 400)
        self.assertIn("address", r.json()["error"])


class BotButton(WebBase):
    quick_login = False

    def test_menu_has_the_button_only_when_asked(self):
        plain = quick_menu.send_as_file_menu("rid1", "https://x.com/a")
        with_link = quick_menu.send_as_file_menu("rid1", "https://x.com/a", get_link=True)
        self.assertNotIn("up|link|rid1", [b.callback_data for b in plain.buttons()])
        self.assertIn("up|link|rid1", [b.callback_data for b in with_link.buttons()])
        self.assertIn("up|link|rid2", [b.callback_data for b in quick_menu.sent_menu("https://x.com/a", "rid2").buttons()])
        self.assertNotIn("up|link", str([b.callback_data for b in quick_menu.sent_menu("https://x.com/a").buttons()]))
        self.assertTrue(all(len((b.callback_data or "").encode()) < 64 for b in with_link.buttons()))

    async def deliver(self, uploader_on, **settings):
        if uploader_on:
            uploader.set_global(GOOD)

        class Bot(FakeTelegramBot):
            sent = []

            async def send_video(self, chat_id, file, **kw):
                Bot.sent.append(("video", kw.get("reply_markup")))

            async def send_audio(self, chat_id, file, **kw):
                Bot.sent.append(("audio", kw.get("reply_markup")))

            async def send_document(self, chat_id, file, **kw):
                Bot.sent.append(("document", kw.get("reply_markup")))

            async def delete_message(self, *a, **kw):
                pass

        Bot.sent = []
        manager = jm.JobManager(Bot(), 1)
        files = []
        for name in ("a.mp4", "b.mp4"):
            path = self.tmp / name
            path.write_bytes(b"v")
            files.append(path)
        job = jm.Job(rid="rid0000009", user_id=1, chat_id=7, url="https://x.com/v", settings=settings, status_message_id=5, header="Sending")
        await manager._send_files(job, files[:1] if not settings.get("multi") else files)
        return Bot.sent

    def has_link(self, markup):
        return markup is not None and any((b.callback_data or "").startswith("up|link|") for b in markup.buttons())

    async def test_button_shows_when_the_uploader_is_on(self):
        sent = await self.deliver(True, mode="video")
        self.assertTrue(self.has_link(sent[0][1]))

    async def test_no_button_when_off_or_for_tools_or_images(self):
        self.assertFalse(self.has_link((await self.deliver(False, mode="video"))[0][1]))
        sent = await self.deliver(True, mode="video", tool="trim")
        self.assertTrue(all(not self.has_link(m) for _, m in sent))

    async def test_with_several_files_only_the_last_has_it(self):
        sent = await self.deliver(True, mode="video", multi=True)
        self.assertEqual([self.has_link(m) for _, m in sent], [False, True])

    async def test_cached_files_are_only_handed_to_their_owner(self):
        manager = jm.JobManager(object(), 1)
        path = self.tmp / "kept.mp4"
        path.write_bytes(b"v")
        manager._recent_files["r1"] = [{"path": path, "url": "u", "title": "t", "name": "kept.mp4"}]
        manager._owners["r1"] = 5
        self.assertEqual(len(manager.cached_files("r1", 5)), 1)
        self.assertEqual(manager.cached_files("r1", 6), [])
        self.assertEqual(manager.cached_files("nope", 5), [])
        path.unlink()
        self.assertEqual(manager.cached_files("r1", 5), [])

    async def callback(self, user_id, data, files_for_owner=5):
        sent = []

        class Query:
            def __init__(s):
                s.data = data
                s.message = type("M", (), {"chat_id": 7, "message_id": 1})()
                s.answers = []

            async def answer(s, *a, **k):
                s.answers.append((a, k))

        query = Query()
        update = type("U", (), {"callback_query": query, "effective_user": type("X", (), {"id": user_id})()})()

        class Bot:
            async def send_message(s, chat_id, text, **kw):
                sent.append(text)
        context = type("C", (), {"bot": Bot()})()
        await main.uploader_callback(update, context)
        return query, sent

    async def test_callback_uploads_and_replies_with_links(self):
        uploader.set_global(GOOD)
        path = self.tmp / "kept.mp4"
        path.write_bytes(b"v")
        manager = jm.JobManager(object(), 1)
        manager._recent_files["r1"] = [{"path": path, "url": "u", "title": "t", "name": "kept <b>.mp4"}]
        manager._owners["r1"] = 5

        async def post(*a, **k):
            return 200, '{"data": {"url": "https://cdn.example/q"}}'
        self.addCleanup(setattr, uploader, "_post", uploader._post)
        uploader._post = post
        self.addCleanup(setattr, main, "job_manager", main.job_manager)
        main.job_manager = manager
        query, sent = await self.callback(5, "up|link|r1")
        self.assertIn("https://cdn.example/q", sent[0])
        self.assertIn("&lt;b&gt;", sent[0])                              # the file name is escaped in the HTML message
        query, sent = await self.callback(6, "up|link|r1")               # somebody else
        self.assertEqual(sent, [])
        self.assertTrue(query.answers[0][1].get("show_alert"))

    async def test_callback_reports_uploader_errors_in_chat(self):
        path = self.tmp / "kept.mp4"
        path.write_bytes(b"v")
        manager = jm.JobManager(object(), 1)
        manager._recent_files["r1"] = [{"path": path, "url": "u", "title": "t", "name": "kept.mp4"}]
        manager._owners["r1"] = 5
        self.addCleanup(setattr, main, "job_manager", main.job_manager)
        main.job_manager = manager
        query, sent = await self.callback(5, "up|link|r1")               # uploader not configured
        self.assertIn("isn't set up", sent[0])

    async def test_one_upload_at_a_time_per_person(self):
        uploader.set_global(GOOD)
        path = self.tmp / "kept.mp4"
        path.write_bytes(b"v")
        manager = jm.JobManager(object(), 1)
        manager._recent_files["r1"] = [{"path": path, "url": "u", "title": "t", "name": "kept.mp4"}]
        manager._owners["r1"] = 5
        gate = asyncio.Event()

        async def post(*a, **k):
            await gate.wait()
            return 200, '{"data": {"url": "https://cdn.example/q"}}'
        self.addCleanup(setattr, uploader, "_post", uploader._post)
        uploader._post = post
        self.addCleanup(setattr, main, "job_manager", main.job_manager)
        main.job_manager = manager
        first = asyncio.create_task(self.callback(5, "up|link|r1"))
        await asyncio.sleep(0.1)
        query, sent = await self.callback(5, "up|link|r1")
        self.assertEqual(sent, [])
        self.assertIn("Already", query.answers[0][0][0])
        gate.set()
        await first
        self.assertNotIn(5, main._uploading)


if __name__ == "__main__":
    import unittest
    unittest.main()
