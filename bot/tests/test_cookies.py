"""
Cookies are per site inside one file per person: uploading Instagram's must not wipe YouTube's, an upload that is
not a cookies file must not destroy the one that works, and every site can be removed on its own.
"""
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import config  # noqa: E402
import main  # noqa: E402
from downloader import cookies as ck  # noqa: E402
from tests.test_sections_flow import FakeBot, FakeContext, FakeMessage, FakeQuery, FakeUpdate, datas, labels  # noqa: E402

USER = 42
FUTURE = 4102444800
PAST = 1000000000


def line(domain, name, value="v", expiry=FUTURE, http_only=False):
    prefix = "#HttpOnly_" if http_only else ""
    return f"{prefix}{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}"


def netscape(*lines):
    return "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"


YOUTUBE = netscape(line(".youtube.com", "LOGIN_INFO"), line(".google.com", "SID"), line("accounts.google.com", "SAPISID", http_only=True),
                   line(".youtube.com", "PREF"))
INSTAGRAM = netscape(line(".instagram.com", "sessionid", "new"), line(".instagram.com", "csrftoken"))


class Sites(unittest.TestCase):
    def test_which_site_a_cookie_belongs_to(self):
        cases = {".youtube.com": "youtube.com", "accounts.google.com": "youtube.com", ".google.de": "youtube.com",
                 "www.google.co.uk": "youtube.com", ".googlevideo.com": "youtube.com", "#HttpOnly_.instagram.com": "instagram.com",
                 ".twitter.com": "x.com", "x.com": "x.com", "news.bbc.co.uk": "bbc.co.uk", "notgoogle.com": "notgoogle.com",
                 "google.evil.com": "evil.com", "localhost": "localhost"}
        for domain, site in cases.items():
            self.assertEqual(ck.site_of(domain), site, domain)

    def test_labels_are_friendly_and_fall_back_to_the_domain(self):
        self.assertEqual((ck.site_label("youtube.com"), ck.site_label("x.com"), ck.site_label("bbc.co.uk")), ("YouTube", "X", "bbc.co.uk"))


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ck-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.mine = self.dir / "42.txt"

    def upload(self, content, name="up.txt"):
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_the_first_upload_creates_the_file_privately(self):
        result = ck.merge_upload(self.mine, self.upload(YOUTUBE))
        self.assertEqual((result.added, result.replaced, result.kept), (["youtube.com"], [], []))
        self.assertEqual(stat.S_IMODE(os.stat(self.mine).st_mode), 0o600)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_another_site_is_added_and_the_first_is_kept(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        result = ck.merge_upload(self.mine, self.upload(INSTAGRAM))
        self.assertEqual((result.added, result.replaced, result.kept), (["instagram.com"], [], ["youtube.com"]))
        text = self.mine.read_text()
        self.assertIn("LOGIN_INFO", text)                      # YouTube survived
        self.assertIn("sessionid", text)
        self.assertEqual([i.site for i in ck.list_sites(self.mine)], ["instagram.com", "youtube.com"])

    def test_uploading_a_site_again_replaces_only_that_site(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        ck.merge_upload(self.mine, self.upload(INSTAGRAM))
        fresh = netscape(line(".instagram.com", "sessionid", "FRESH"))
        result = ck.merge_upload(self.mine, self.upload(fresh))
        self.assertEqual((result.replaced, result.kept), (["instagram.com"], ["youtube.com"]))
        text = self.mine.read_text()
        self.assertIn("FRESH", text)
        self.assertNotIn("csrftoken", text)                    # the old Instagram block is gone, not duplicated
        self.assertEqual(text.count("sessionid"), 1)
        self.assertIn("LOGIN_INFO", text)

    def test_a_youtube_export_replaces_all_of_the_youtube_and_google_cookies_together(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        newer = netscape(line(".youtube.com", "LOGIN_INFO", "NEWER"), line(".google.com", "SID", "NEWER"))
        ck.merge_upload(self.mine, self.upload(newer))
        text = self.mine.read_text()
        self.assertEqual(text.count("NEWER"), 2)
        self.assertNotIn("SAPISID", text)
        self.assertNotIn("PREF", text)

    def test_httponly_lines_survive_exactly(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        self.assertIn("#HttpOnly_accounts.google.com\tTRUE\t/\tTRUE\t" + str(FUTURE) + "\tSAPISID\tv", self.mine.read_text())

    def test_a_file_without_cookies_changes_nothing(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        before = self.mine.read_bytes()
        for junk in ("hello world", "", "# Netscape HTTP Cookie File\n", "a\tb\tc\n"):
            self.assertIsNone(ck.merge_upload(self.mine, self.upload(junk)), junk)
        self.assertEqual(self.mine.read_bytes(), before)

    def test_a_missing_login_cookie_is_reported_for_instagram_and_x(self):
        out = ck.merge_upload(self.mine, self.upload(netscape(line(".instagram.com", "csrftoken"), line(".x.com", "guest_id"))))
        self.assertEqual(sorted(out.no_login), ["instagram.com", "x.com"])
        ok = ck.merge_upload(self.mine, self.upload(netscape(line(".instagram.com", "sessionid"), line(".twitter.com", "auth_token"))))
        self.assertEqual(ok.no_login, [])

    def test_the_list_counts_cookies_and_flags_expired_sites(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        ck.merge_upload(self.mine, self.upload(netscape(line(".instagram.com", "sessionid", expiry=PAST), line(".instagram.com", "a", expiry=PAST))))
        ck.merge_upload(self.mine, self.upload(netscape(line(".reddit.com", "s", expiry=0), line(".reddit.com", "t", expiry=PAST))))
        info = {i.site: i for i in ck.list_sites(self.mine)}
        self.assertEqual((info["youtube.com"].count, info["youtube.com"].expired), (4, False))
        self.assertEqual((info["instagram.com"].count, info["instagram.com"].expired), (2, True))
        self.assertFalse(info["reddit.com"].expired)           # a session cookie never expires on its own

    def test_removing_one_site_keeps_the_others_and_the_last_one_deletes_the_file(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        ck.merge_upload(self.mine, self.upload(INSTAGRAM))
        self.assertTrue(ck.remove_site(self.mine, "instagram.com"))
        self.assertEqual([i.site for i in ck.list_sites(self.mine)], ["youtube.com"])
        self.assertFalse(ck.remove_site(self.mine, "instagram.com"))
        self.assertFalse(ck.remove_site(self.mine, "nothing.example"))
        self.assertTrue(ck.remove_site(self.mine, "youtube.com"))
        self.assertFalse(self.mine.exists())

    def test_what_yt_dlp_and_gallery_dl_read_is_still_a_valid_netscape_file(self):
        ck.merge_upload(self.mine, self.upload(YOUTUBE))
        ck.merge_upload(self.mine, self.upload(INSTAGRAM))
        first = self.mine.read_text().splitlines()[0]
        self.assertEqual(first, "# Netscape HTTP Cookie File")
        self.assertTrue(ck.inspect_cookie_file(self.mine)["logged_in"])      # the YouTube login is still detected


class FakeDoc:
    def __init__(self, content: str, file_path=None):
        self.content, self.file_path, self.file_name, self.file_size = content, file_path, "cookies.txt", len(content)

    async def get_file(self):
        return self

    async def download_to_drive(self, custom_path=None, **kw):
        Path(custom_path).write_text(self.content, encoding="utf-8")


class Reply:
    def __init__(self):
        self.texts: list[str] = []

    async def reply_text(self, text, **kw):
        self.texts.append(text)


class Handlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ckh-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for name, value in (("COOKIES_DIR", str(self.dir)),):
            self.addCleanup(setattr, config, name, getattr(config, name))
            setattr(config, name, value)
        self.settings_calls = []
        self.addCleanup(setattr, main, "update_setting", main.update_setting)
        main.update_setting = lambda *a, **k: self.settings_calls.append(a)

        async def allow(*a, **k):
            return True
        self.addCleanup(setattr, main, "gate", main.gate)
        self.addCleanup(setattr, main, "gate_callback", main.gate_callback)
        main.gate, main.gate_callback = allow, allow
        self.bot = FakeBot()

    async def send(self, content):
        update = FakeUpdate(self.bot)
        update.message = FakeMessage()
        reply = Reply()
        update.message.reply_text = reply.reply_text
        update.message.document = FakeDoc(content)
        await main.cookies_file_handler(update, FakeContext(self.bot))
        return reply.texts[-1]

    def mine(self):
        return self.dir / f"{USER}.txt"

    async def test_sending_instagram_after_youtube_keeps_youtube(self):
        await self.send(YOUTUBE)
        text = await self.send(INSTAGRAM)
        self.assertIn("Cookies saved for Instagram", text)
        self.assertIn("other cookies (YouTube) were kept", text)
        self.assertEqual([i.site for i in ck.list_sites(self.mine())], ["instagram.com", "youtube.com"])
        self.assertTrue(self.settings_calls)                                     # cookies switched on

    async def test_a_second_upload_of_the_same_site_says_replaced_content_and_keeps_the_rest(self):
        await self.send(YOUTUBE)
        await self.send(INSTAGRAM)
        text = await self.send(netscape(line(".instagram.com", "sessionid", "FRESH")))
        self.assertIn("FRESH", self.mine().read_text())
        self.assertIn("YouTube", text)

    async def test_something_that_is_not_a_cookies_file_leaves_the_working_one_alone(self):
        await self.send(YOUTUBE)
        before = self.mine().read_bytes()
        text = await self.send("this is not a cookies file")
        self.assertIn("nothing was changed", text)
        self.assertEqual(self.mine().read_bytes(), before)                       # it used to overwrite and destroy it
        self.assertEqual(list(self.dir.glob("*.upload")), [])

    async def test_no_temporary_upload_is_left_behind(self):
        await self.send(INSTAGRAM)
        self.assertEqual([p.name for p in self.dir.iterdir()], [f"{USER}.txt"])

    async def test_the_existing_warnings_still_work(self):
        text = await self.send(netscape(line(".youtube.com", "PREF")))
        self.assertIn("no YouTube login", text)
        text = await self.send(netscape(line(".youtube.com", "LOGIN_INFO", expiry=PAST), line(".google.com", "SID", expiry=PAST)))
        self.assertIn("already expired", text)
        text = await self.send(netscape(line(".instagram.com", "csrftoken")))
        self.assertIn("no Instagram login", text)

    # ---- the screen
    async def tap(self, data):
        query = FakeQuery(data, self.bot)
        await main.settings_callback(FakeUpdate(self.bot, query), FakeContext(self.bot))
        return self.bot.edits[-1]

    async def test_the_cookies_screen_lists_sites_with_a_remove_button_each(self):
        await self.send(YOUTUBE)
        await self.send(INSTAGRAM)
        edit = await self.tap("nav|cookies")
        self.assertIn("Your cookies", edit["text"])
        self.assertIn("Instagram · 2 cookies", edit["text"])
        self.assertIn("YouTube · 4 cookies", edit["text"])
        self.assertEqual(labels(edit["markup"]), ["🗑 Remove Instagram", "🗑 Remove YouTube", "← Back"])
        self.assertEqual(datas(edit["markup"]), ["ck|rm|instagram.com", "ck|rm|youtube.com", "nav|main"])

    async def test_removing_one_site_leaves_the_other_and_refreshes_the_screen(self):
        await self.send(YOUTUBE)
        await self.send(INSTAGRAM)
        edit = await self.tap("ck|rm|instagram.com")
        self.assertNotIn("Instagram", edit["text"])
        self.assertIn("YouTube", edit["text"])
        self.assertEqual([i.site for i in ck.list_sites(self.mine())], ["youtube.com"])

    async def test_an_empty_screen_says_so_and_a_stale_remove_button_is_harmless(self):
        edit = await self.tap("ck|rm|instagram.com")
        self.assertIn("None yet", edit["text"])
        self.assertEqual(labels(edit["markup"]), ["← Back"])

    async def test_nobody_can_remove_somebody_elses_cookies(self):
        other = self.dir / "999.txt"
        ck.merge_upload(other, self.write_tmp(INSTAGRAM))
        await self.tap("ck|rm|instagram.com")                 # the tapping user is USER (42)
        self.assertTrue(other.exists())

    def test_the_remove_buttons_reach_the_settings_handler(self):
        import inspect
        import re
        source = inspect.getsource(main)
        pattern = re.search(r'CallbackQueryHandler\(settings_callback, pattern=r"([^"]+)"\)', source).group(1)
        for data in ("ck|rm|instagram.com", "nav|cookies", "s|adhd_mode|1"):
            self.assertTrue(re.match(pattern, data), data)

    def write_tmp(self, content):
        path = self.dir / "tmp_in.txt"
        path.write_text(content)
        return path


if __name__ == "__main__":
    unittest.main()
