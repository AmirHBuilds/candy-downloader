"""Warning someone when their YouTube cookies stop working."""
import contextlib
import tempfile
import time
import unittest
from pathlib import Path

from tests import _env  # noqa: F401

from downloader import cookie_health  # noqa: E402
from downloader.cookie_health import CookieWatch, classify  # noqa: E402
from downloader.cookies import inspect_cookie_file  # noqa: E402

BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
DEAD = "WARNING: The provided YouTube account cookies are no longer valid. They have likely been rotated"


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify(DEAD), "dead")
        self.assertEqual(classify(BOT_CHECK), "login")
        self.assertEqual(classify("Sign in to confirm your age"), "login")
        for unrelated in ("HTTP Error 403", "boom", "timed out", "", None):
            self.assertIsNone(classify(unrelated))


class Watch(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.watch = CookieWatch(clock=lambda: self.now, threshold=2, cooldown_seconds=100)

    def test_one_sign_in_failure_is_not_enough_two_are(self):
        self.assertFalse(self.watch.record_failure(1, BOT_CHECK))
        self.assertTrue(self.watch.record_failure(1, BOT_CHECK))

    def test_an_explicit_message_warns_at_once(self):
        self.assertTrue(self.watch.record_failure(1, DEAD))

    def test_unrelated_errors_neither_count_nor_reset(self):
        self.assertFalse(self.watch.record_failure(1, BOT_CHECK))
        self.assertFalse(self.watch.record_failure(1, "network down"))
        self.assertTrue(self.watch.record_failure(1, BOT_CHECK))        # the first one still counted

    def test_a_success_starts_the_count_over(self):
        self.watch.record_failure(1, BOT_CHECK)
        self.watch.record_success(1)
        self.assertFalse(self.watch.record_failure(1, BOT_CHECK))

    def test_one_warning_then_quiet_until_the_cooldown_passes(self):
        self.assertTrue(self.watch.record_failure(1, DEAD))
        self.assertFalse(self.watch.record_failure(1, DEAD))
        self.now += 101
        self.assertTrue(self.watch.record_failure(1, DEAD))

    def test_people_are_tracked_separately(self):
        self.watch.record_failure(1, BOT_CHECK)
        self.assertFalse(self.watch.record_failure(2, BOT_CHECK))
        self.assertTrue(self.watch.record_failure(1, BOT_CHECK))

    def test_the_alert_names_the_fix_and_the_ip_block_possibility(self):
        self.assertIn("/cookies", cookie_health.COOKIE_ALERT)
        self.assertIn("blocking", cookie_health.COOKIE_ALERT)


class CookiesInUse(unittest.TestCase):
    def setUp(self):
        self.original = cookie_health.cookie_file_for
        self.addCleanup(setattr, cookie_health, "cookie_file_for", self.original)
        cookie_health.cookie_file_for = lambda uid, settings: "/x/cookies.txt" if settings.get("on") else None

    def test_only_youtube_with_cookies_enabled_counts(self):
        yt = "https://www.youtube.com/watch?v=a"
        self.assertTrue(cookie_health.cookies_in_use(1, {"on": True}, yt))
        self.assertTrue(cookie_health.cookies_in_use(1, {"on": True}, "https://youtu.be/a"))
        self.assertFalse(cookie_health.cookies_in_use(1, {"on": False}, yt))
        self.assertFalse(cookie_health.cookies_in_use(1, {"on": True}, "https://vimeo.com/1"))
        self.assertFalse(cookie_health.cookies_in_use(1, {"on": True}, "https://notyoutube.com/x"))


class UploadedFileCheck(unittest.TestCase):
    def write(self, *cookies):
        path = Path(tempfile.mkdtemp()) / "c.txt"
        path.write_text("# Netscape HTTP Cookie File\n" + "".join(
            f".youtube.com\tTRUE\t/\tTRUE\t{exp}\t{name}\tvalue\n" for name, exp in cookies))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def test_expired_login_cookies_are_flagged(self):
        past = int(time.time()) - 86400
        check = inspect_cookie_file(self.write(("SID", past), ("__Secure-3PSID", past)))
        self.assertTrue(check["logged_in"])
        self.assertTrue(check["expired"])

    def test_valid_or_session_cookies_are_not(self):
        future = int(time.time()) + 86400 * 300
        self.assertFalse(inspect_cookie_file(self.write(("SID", future)))["expired"])
        self.assertFalse(inspect_cookie_file(self.write(("SID", 0)))["expired"])             # session cookie
        self.assertFalse(inspect_cookie_file(self.write(("SID", int(time.time()) - 5), ("SAPISID", future)))["expired"])

    def test_no_login_cookies_means_not_expired(self):
        self.assertFalse(inspect_cookie_file(self.write(("PREF", int(time.time()) - 5)))["expired"])


if __name__ == "__main__":
    unittest.main()
