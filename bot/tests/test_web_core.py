"""Web security primitives and the accounts database: passwords, tokens, rate limits, lockout, sessions, roles."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import _env  # noqa: F401  (must come first)

from web import accounts, security  # noqa: E402


class Passwords(unittest.TestCase):
    def test_round_trip_and_wrong(self):
        stored = security.hash_password("a good password")
        self.assertTrue(security.verify_password("a good password", stored))
        self.assertFalse(security.verify_password("a good passwore", stored))

    def test_salted_and_not_plain(self):
        a, b = security.hash_password("same password!"), security.hash_password("same password!")
        self.assertNotEqual(a, b)
        self.assertNotIn("same password", a)

    def test_garbage_hash_is_a_no_not_a_crash(self):
        for junk in ("", "x", "scrypt$1$2", "md5$a$b$c$d$e", "scrypt$x$y$z$!!$!!"):
            self.assertFalse(security.verify_password("p", junk))

    def test_policy(self):
        self.assertTrue(security.check_password_policy("short"))
        self.assertTrue(security.check_password_policy("x" * 201))
        self.assertTrue(security.check_password_policy("aaaaaaaaaaaa"))
        self.assertTrue(security.check_password_policy("Alice12345", "alice12345"))
        self.assertEqual(security.check_password_policy("correct horse battery", "alice"), "")

    def test_generated_passwords_pass_the_policy_and_differ(self):
        seen = {security.generate_password() for _ in range(20)}
        self.assertEqual(len(seen), 20)
        for pw in seen:
            self.assertEqual(security.check_password_policy(pw), "")

    def test_tokens_are_hashed_and_long(self):
        token = security.new_token()
        self.assertGreaterEqual(len(token), 40)
        self.assertNotIn(token, security.hash_token(token))
        self.assertEqual(security.hash_token(token), security.hash_token(token))


class Limiter(unittest.TestCase):
    def test_sliding_window(self):
        now = [0.0]
        limiter = security.RateLimiter(3, 10, clock=lambda: now[0])
        self.assertTrue(all(limiter.hit("k") for _ in range(3)))
        self.assertFalse(limiter.hit("k"))
        self.assertTrue(limiter.blocked("k"))
        self.assertTrue(limiter.hit("other"))
        now[0] = 10.5
        self.assertFalse(limiter.blocked("k"))
        self.assertTrue(limiter.hit("k"))

    def test_retry_after_and_reset(self):
        now = [0.0]
        limiter = security.RateLimiter(1, 60, clock=lambda: now[0])
        limiter.hit("k")
        now[0] = 20
        self.assertTrue(30 <= limiter.retry_after("k") <= 42)
        limiter.reset("k")
        self.assertFalse(limiter.blocked("k"))


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="acc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name, value in (("DB_PATH", str(self.tmp / "c.db")), ("DATA_DIR", str(self.tmp))):
            self.addCleanup(setattr, accounts, name, getattr(accounts, name))
            setattr(accounts, name, value)


class Accounts(DbCase):
    def test_create_and_authenticate(self):
        accounts.create_account("Mia", "long enough pw")
        self.assertIsNotNone(accounts.authenticate("mia", "long enough pw"))       # names ignore case
        self.assertIsNone(accounts.authenticate("mia", "long enough pX"))
        self.assertIsNone(accounts.authenticate("nobody", "long enough pw"))

    def test_hash_is_what_is_stored(self):
        accounts.create_account("mia", "long enough pw")
        import sqlite3
        raw = sqlite3.connect(accounts.DB_PATH).execute("SELECT pw_hash FROM web_accounts").fetchone()[0]
        self.assertNotIn("long enough pw", raw)

    def test_username_rules_and_duplicates(self):
        for bad in ("ab", "has space", "x" * 40, "-lead", "emoji🍬name", ""):
            with self.assertRaises(accounts.AccountError):
                accounts.create_account(bad, "long enough pw")
        accounts.create_account("mia", "long enough pw")
        with self.assertRaises(accounts.AccountError):
            accounts.create_account("MIA", "long enough pw")
        with self.assertRaises(accounts.AccountError):
            accounts.create_account("short", "tiny")

    def test_five_wrong_passwords_lock_even_the_right_one(self):
        accounts.create_account("mia", "long enough pw")
        for _ in range(accounts.MAX_FAILED_LOGINS):
            self.assertIsNone(accounts.authenticate("mia", "wrong wrong wrong"))
        self.assertTrue(accounts.is_locked("mia"))
        self.assertIsNone(accounts.authenticate("mia", "long enough pw"))
        with mock.patch("time.time", return_value=time.time() + accounts.LOCKOUT_SECONDS + 5):
            self.assertIsNotNone(accounts.authenticate("mia", "long enough pw"))

    def test_success_resets_the_failure_count(self):
        accounts.create_account("mia", "long enough pw")
        for _ in range(accounts.MAX_FAILED_LOGINS - 1):
            accounts.authenticate("mia", "wrong wrong wrong")
        self.assertIsNotNone(accounts.authenticate("mia", "long enough pw"))
        for _ in range(accounts.MAX_FAILED_LOGINS - 1):
            accounts.authenticate("mia", "wrong wrong wrong")
        self.assertFalse(accounts.is_locked("mia"))

    def test_disabled_cannot_log_in(self):
        acc = accounts.create_account("mia", "long enough pw")
        accounts.create_account("boss", "long enough pw", role="admin")
        accounts.set_disabled(acc.id, True)
        self.assertIsNone(accounts.authenticate("mia", "long enough pw"))

    def test_user_id_is_telegram_id_or_negative(self):
        linked = accounts.create_account("tgy", "long enough pw", telegram_id=12345)
        plain = accounts.create_account("webby", "long enough pw")
        self.assertEqual(linked.user_id, 12345)
        self.assertEqual(plain.user_id, -plain.id)
        self.assertEqual(accounts.account_by_user_id(12345).id, linked.id)
        self.assertEqual(accounts.account_by_user_id(plain.user_id).id, plain.id)
        with self.assertRaises(accounts.AccountError):
            accounts.create_account("tgy2", "long enough pw", telegram_id=12345)


class Sessions(DbCase):
    def setUp(self):
        super().setUp()
        self.acc = accounts.create_account("mia", "long enough pw")
        accounts.create_account("boss", "long enough pw", role="admin")

    def test_session_round_trip_and_only_hash_stored(self):
        token, csrf = accounts.create_session(self.acc.id)
        found = accounts.session_account(token)
        self.assertEqual((found[0].id, found[1]), (self.acc.id, csrf))
        import sqlite3
        stored = sqlite3.connect(accounts.DB_PATH).execute("SELECT token_hash FROM web_sessions").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertIsNone(accounts.session_account(token + "x"))
        self.assertIsNone(accounts.session_account(""))

    def test_idle_and_absolute_expiry(self):
        token, _ = accounts.create_session(self.acc.id)
        with mock.patch("time.time", return_value=time.time() + accounts.SESSION_IDLE_SECONDS + 10):
            self.assertIsNone(accounts.session_account(token))
        token, _ = accounts.create_session(self.acc.id)
        now = time.time()
        for step in range(1, 40):                         # stays alive by being used, but not past the absolute limit
            with mock.patch("time.time", return_value=now + step * 86400):
                alive = accounts.session_account(token)
            if step * 86400 > accounts.SESSION_MAX_SECONDS:
                self.assertIsNone(alive)
                break
        else:
            self.fail("session never expired")

    def test_password_change_signs_everyone_out(self):
        token, _ = accounts.create_session(self.acc.id)
        accounts.set_password(self.acc.id, "another long pw")
        self.assertIsNone(accounts.session_account(token))
        self.assertIsNone(accounts.authenticate("mia", "long enough pw"))
        self.assertIsNotNone(accounts.authenticate("mia", "another long pw"))

    def test_disable_kills_sessions(self):
        token, _ = accounts.create_session(self.acc.id)
        accounts.set_disabled(self.acc.id, True)
        self.assertIsNone(accounts.session_account(token))

    def test_logout(self):
        token, _ = accounts.create_session(self.acc.id)
        accounts.end_session(token)
        self.assertIsNone(accounts.session_account(token))

    def test_session_cap_drops_the_oldest(self):
        tokens = []
        for i in range(accounts.MAX_SESSIONS_PER_ACCOUNT + 2):
            with mock.patch("time.time", return_value=time.time() + i):
                tokens.append(accounts.create_session(self.acc.id)[0])
        self.assertIsNone(accounts.session_account(tokens[0]))
        self.assertIsNotNone(accounts.session_account(tokens[-1]))


class Roles(DbCase):
    def test_last_admin_is_protected(self):
        boss = accounts.create_account("boss", "long enough pw", role="admin")
        for action in (lambda: accounts.set_role(boss.id, "user"), lambda: accounts.set_disabled(boss.id, True),
                       lambda: accounts.delete_account(boss.id)):
            with self.assertRaises(accounts.AccountError):
                action()
        second = accounts.create_account("boss2", "long enough pw", role="admin")
        accounts.set_role(boss.id, "user")                    # fine now: someone else is admin
        with self.assertRaises(accounts.AccountError):
            accounts.delete_account(second.id)

    def test_bootstrap_admin_only_when_none_exists(self):
        first = accounts.ensure_bootstrap_admin("owner", "long enough pw")
        self.assertEqual(first.role, "admin")
        self.assertIsNone(accounts.ensure_bootstrap_admin("owner2", "long enough pw"))
        self.assertIsNone(accounts.ensure_bootstrap_admin("", ""))

    def test_issue_for_telegram_creates_then_resets(self):
        acc, password, created = accounts.issue_for_telegram(777, "Cool.Name", "user")
        self.assertTrue(created and acc.must_change and acc.telegram_id == 777)
        self.assertIsNotNone(accounts.authenticate(acc.username, password))
        again, password2, created2 = accounts.issue_for_telegram(777, "other", "admin")
        self.assertFalse(created2)
        self.assertEqual((again.id, again.username, again.role), (acc.id, acc.username, "user"))   # role/name unchanged
        self.assertIsNone(accounts.authenticate(acc.username, password))
        self.assertIsNotNone(accounts.authenticate(acc.username, password2))

    def test_issue_for_telegram_handles_a_taken_name_and_no_username(self):
        accounts.create_account("tg5", "long enough pw")
        acc, _, _ = accounts.issue_for_telegram(5, None)
        self.assertNotEqual(acc.username.lower(), "tg5")
        self.assertTrue(accounts.USERNAME_RE.match(acc.username))

    def test_reset_unblocks_a_disabled_telegram_account(self):
        acc, _, _ = accounts.issue_for_telegram(9, "nine")
        accounts.create_account("boss", "long enough pw", role="admin")
        accounts.set_disabled(acc.id, True)
        again, pw, _ = accounts.issue_for_telegram(9, "nine")
        self.assertFalse(again.disabled)


if __name__ == "__main__":
    unittest.main()
