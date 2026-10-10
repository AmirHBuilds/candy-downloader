"""Gaps found by mutation-checking the web app: each test here failed to notice a deliberately broken rule."""
import sqlite3

from tests.webharness import PASSWORD, Client, WebBase  # noqa: E402  (imports _env first)
from tests import test_web_core as core  # noqa: E402

from starlette.requests import Request  # noqa: E402

from web import accounts  # noqa: E402


class SessionRules(core.DbCase):
    def setUp(self):
        super().setUp()
        self.acc = accounts.create_account("mia", "long enough pw")
        accounts.create_account("boss", "long enough pw", role="admin")

    def sessions_left(self):
        conn = sqlite3.connect(accounts.DB_PATH)
        try:
            return conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0]
        finally:
            conn.close()

    def test_a_disabled_account_is_refused_even_if_its_session_row_survived(self):
        token, _ = accounts.create_session(self.acc.id)
        conn = sqlite3.connect(accounts.DB_PATH)
        conn.execute("UPDATE web_accounts SET disabled = 1 WHERE id = ?", (self.acc.id,))     # bypass set_disabled
        conn.commit()
        conn.close()
        self.assertIsNone(accounts.session_account(token))
        self.assertEqual(self.sessions_left(), 0)                                             # and the stale row is removed

    def test_blocking_an_account_deletes_its_sessions_at_once(self):
        accounts.create_session(self.acc.id)
        accounts.create_session(self.acc.id)
        self.assertEqual(self.sessions_left(), 2)
        accounts.set_disabled(self.acc.id, True)
        self.assertEqual(self.sessions_left(), 0)

    def test_unblocking_keeps_nothing_old(self):
        accounts.set_disabled(self.acc.id, True)
        accounts.set_disabled(self.acc.id, False)
        self.assertEqual(self.sessions_left(), 0)

    def test_a_telegram_password_reset_forces_a_new_password(self):
        account, password, _ = accounts.issue_for_telegram(31, "someone")
        accounts.set_password(account.id, "my own chosen password")             # they chose their own
        self.assertFalse(accounts.get_account(account.id).must_change)
        again, new_password, _ = accounts.issue_for_telegram(31, "someone")
        self.assertTrue(again.must_change)                                       # a one-time password must be replaced


class MoreHttp(WebBase):
    async def test_an_admin_cannot_delete_or_block_themselves_even_with_another_admin(self):
        accounts.create_account("boss2", PASSWORD, role="admin")
        self.assertEqual((await self.a.delete(f"/api/admin/accounts/{self.alice.id}")).status, 400)
        self.assertEqual((await self.a.patch(f"/api/admin/accounts/{self.alice.id}", {"disabled": True})).status, 400)
        self.assertEqual((await self.a.get("/api/me")).status, 200)
        self.assertEqual(accounts.get_account(self.alice.id).disabled, False)

    async def test_the_static_handler_refuses_odd_names_by_itself(self):
        route = next(r for r in self.app.app.routes if getattr(r, "path", "") == "/static/{name}")
        for name in ("../app.py", "..", "a/b", "app.py\x00", "", ".."):
            scope = {"type": "http", "method": "GET", "path": "/static/x", "headers": [], "path_params": {"name": name},
                     "query_string": b"", "client": ("1.1.1.1", 1), "server": ("x", 1), "scheme": "https"}
            response = await route.endpoint(Request(scope))
            self.assertEqual(response.status_code, 404, repr(name))
        scope["path_params"] = {"name": "app.css"}
        self.assertEqual((await route.endpoint(Request(scope))).status_code, 200)

    async def test_files_in_the_app_folder_are_not_served_as_static(self):
        for name in ("app.py", "accounts.py", "security.py", "__init__.py", "shares.py"):
            self.assertEqual((await self.b.get("/static/" + name)).status, 404, name)

    async def test_a_note_belongs_to_its_own_job_when_two_run_at_once(self):
        import asyncio
        from pathlib import Path
        from jobqueue import job_manager as jm

        async def fake(url, workspace, settings, user_id, cb, cancel_event=None):
            path = Path(workspace) / ("a.mp4" if url.endswith("/a") else "b.mp4")
            path.write_bytes(b"v")
            if url.endswith("/a"):
                await asyncio.sleep(0.4)                       # a is still running when b is queued behind it
                settings.setdefault("delivery_notes", []).append("note for A")
            return [path]
        self.addCleanup(setattr, jm, "dispatch_download", jm.dispatch_download)
        jm.dispatch_download = fake
        a = (await self.b.post("/api/download", {"url": "https://example.com/a", "kind": "simple"})).json()["rid"]
        b = (await self.b.post("/api/download", {"url": "https://example.com/b", "kind": "simple"})).json()["rid"]
        job_a = await self.wait_for(self.b, a)
        job_b = await self.wait_for(self.b, b)
        self.assertEqual(job_a["notes"], ["note for A"])
        self.assertEqual(job_b["notes"], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
