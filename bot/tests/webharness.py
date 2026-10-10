"""Shared pieces for the web tests: a minimal ASGI client (no httpx needed) and a fully wired app on a temp folder."""
import asyncio
import json as jsonlib
import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from tests import _env  # noqa: F401  (must come first)

import config  # noqa: E402
from downloader import tools  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from settings import access_control, user_settings  # noqa: E402
from utils import cleanup  # noqa: E402
from utils.webchat import RoutingBot, deliveries  # noqa: E402
from web import accounts, app as webapp, jobs as webjobs, service, shares  # noqa: E402


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return jsonlib.loads(self.body or b"{}")

    def header(self, name):
        return next((v for k, v in self.headers if k == name.lower()), None)

    def set_cookies(self):
        return [v for k, v in self.headers if k == "set-cookie"]


class Client:
    """Talks to the ASGI app directly, keeps cookies like a browser, and can send the CSRF header for you."""

    def __init__(self, app, ip="203.0.113.5", host="web.test"):
        self.app, self.cookies, self.csrf, self.ip, self.host = app, {}, "", ip, host

    async def request(self, method, path, json=None, body=None, headers=None, csrf=True, origin=None, ip=None):
        parts = urlsplit(path)
        data = body if body is not None else (jsonlib.dumps(json).encode() if json is not None else b"")
        hdrs = {"host": self.host, "x-forwarded-for": ip or self.ip}
        if json is not None:
            hdrs["content-type"] = "application/json"
        if csrf and method not in ("GET", "HEAD") and self.csrf:
            hdrs["x-csrf-token"] = self.csrf
        if self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if origin:
            hdrs["origin"] = origin
        hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
                 "path": parts.path, "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
                 "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
                 "client": ("127.0.0.1", 1234), "server": ("web.test", 443)}
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": data, "more_body": False}
            await asyncio.sleep(3600)

        out = {"status": 0, "headers": [], "body": b""}

        async def send(message):
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
                out["headers"] = [(k.decode().lower(), v.decode()) for k, v in message["headers"]]
            elif message["type"] == "http.response.body":
                out["body"] += message.get("body", b"")

        await self.app(scope, receive, send)
        resp = Resp(out["status"], out["headers"], out["body"])
        for cookie in resp.set_cookies():
            name, _, rest = cookie.partition("=")
            value = rest.split(";")[0]
            if "Max-Age=0" in cookie or value in ('""', ""):
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return resp

    get = lambda self, path, **kw: self.request("GET", path, **kw)       # noqa: E731
    post = lambda self, path, json=None, **kw: self.request("POST", path, json=json, **kw)   # noqa: E731
    put = lambda self, path, json=None, **kw: self.request("PUT", path, json=json, **kw)     # noqa: E731
    patch = lambda self, path, json=None, **kw: self.request("PATCH", path, json=json, **kw)  # noqa: E731
    delete = lambda self, path, **kw: self.request("DELETE", path, **kw)                      # noqa: E731

    async def login(self, username, password):
        resp = await self.post("/api/auth/login", {"username": username, "password": password}, csrf=False)
        if resp.status == 200:
            self.csrf = resp.json()["csrf"]
        return resp


class FakeTelegramBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text="", **kw):
        self.messages.append((chat_id, text))

    async def delete_message(self, *a, **kw):
        pass

    async def edit_message_text(self, *a, **kw):
        pass

    async def edit_message_caption(self, *a, **kw):
        pass

    async def send_video(self, *a, **kw):
        pass

    async def send_audio(self, *a, **kw):
        pass

    async def send_document(self, *a, **kw):
        pass

    async def send_photo(self, *a, **kw):
        pass


PASSWORD = "correct horse battery"


class WebBase(unittest.IsolatedAsyncioTestCase):
    """A temp data folder, a real JobManager (downloads faked), the real app. Accounts: alice (admin), bob (user)."""

    quick_login = True

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="webtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "data").mkdir()
        (self.tmp / "tmp").mkdir()
        (self.tmp / "cookies").mkdir()
        db = str(self.tmp / "data" / "candy.db")
        for module, name, value in ((accounts, "DB_PATH", db), (accounts, "DATA_DIR", str(self.tmp / "data")),
                                    (shares, "DB_PATH", db), (shares, "DATA_DIR", str(self.tmp / "data")),
                                    (config, "WEB_ENABLED", True),
                                    (access_control, "DB_PATH", db), (access_control, "DATA_DIR", str(self.tmp / "data")),
                                    (user_settings, "DB_PATH", db), (user_settings, "DATA_DIR", str(self.tmp / "data")),
                                    (config, "COOKIES_DIR", str(self.tmp / "cookies")), (config, "TMP_DIR", str(self.tmp / "tmp")),
                                    (cleanup, "TMP_DIR", str(self.tmp / "tmp")), (jm, "TMP_DIR", str(self.tmp / "tmp")),
                                    (service, "MAX_UPLOAD_BYTES", 5_000_000)):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)
        counter = iter(range(10_000))
        self.addCleanup(setattr, jm, "new_cache_path", jm.new_cache_path)
        (self.tmp / "cache").mkdir()
        jm.new_cache_path = lambda suffix: self.tmp / "cache" / f"c{next(counter)}{suffix}"
        self.addCleanup(setattr, tools, "store", tools.store)
        tools.store = tools.InputStore(self.tmp / "tmp" / "tools")
        self.addCleanup(deliveries.files.clear)
        self.addCleanup(deliveries.notes.clear)

        self.tg = FakeTelegramBot()
        self.manager = jm.JobManager(RoutingBot(self.tg), max_concurrent=2)
        self.manager.start()
        self.addCleanup(lambda: [w.cancel() for w in self.manager._workers])
        self.jobs = webjobs.WebJobs(self.manager, ttl_seconds=3600, quota_bytes=50_000_000, max_active=3,
                                    root=self.tmp / "tmp" / "web")
        self.sent_to_telegram = []

        async def send_to_telegram(tid, path, name):
            self.sent_to_telegram.append((tid, Path(path).read_bytes(), name))

        self.ctx = webapp.Context(self.jobs, send_to_telegram=send_to_telegram, secure_cookie=True, trust_proxy=True,
                                  public_url="")
        self.app = webapp.build_app(self.ctx)
        self.alice = accounts.create_account("alice", PASSWORD, role="admin")
        self.bob = accounts.create_account("bob", PASSWORD)
        self.a, self.b = Client(self.app), Client(self.app, ip="198.51.100.9")
        if self.quick_login:
            self.assertEqual((await self.a.login("alice", PASSWORD)).status, 200)
            self.assertEqual((await self.b.login("bob", PASSWORD)).status, 200)

    async def wait_for(self, client, rid, states=("done", "failed", "cancelled"), timeout=5):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            jobs = (await client.get("/api/jobs")).json()["jobs"]
            job = next((j for j in jobs if j["rid"] == rid), None)
            if job and job["state"] in states:
                return job
            await asyncio.sleep(0.05)
        self.fail(f"job {rid} didn't reach {states}")

    def fake_download(self, content=b"hello world", name="Song.mp3", fail=None, delay=0.0):
        """Replace the real downloaders: write one file into the job's folder."""
        async def fake(url, workspace, settings, user_id, cb, cancel_event=None):
            if delay:
                await asyncio.sleep(delay)
            if fail:
                raise RuntimeError(fail)
            path = Path(workspace) / name
            path.write_bytes(content)
            return [path]
        self.addCleanup(setattr, jm, "dispatch_download", jm.dispatch_download)
        jm.dispatch_download = fake
