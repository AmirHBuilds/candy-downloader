"""
The web app's HTTP API (Starlette). Everything under /api needs a signed-in session, except logging in.

Security model, in one place:
  * Sessions: random token in an HttpOnly + Secure + SameSite=Strict cookie; only its hash is stored server-side.
  * CSRF: every state-changing request must carry the per-session X-CSRF-Token header AND, when the browser sends an
    Origin header, that origin must be this site. Two independent checks.
  * Login: per-address and per-username rate limits, account lockout, identical answers for "no such user" and
    "wrong password", and equal time for both.
  * Authorisation: every job, file and tool upload is looked up through the signed-in account, so another
    person's id is simply "not found". Admin routes check the role on every call.
  * Files are only ever served by (job id, index) from the web store - the client never supplies a path.
  * Response headers: strict CSP (no inline script, no framing), nosniff, no-store on API answers.
"""
import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

import config
from downloader import tools, warp_control
from settings import access_control as ac
from settings.user_settings import reset_settings, update_setting
from utils.webchat import deliveries
from web import accounts, service, shares
from web.jobs import SubmitError, WebJobs
from web.security import RateLimiter, generate_password, safe_equal
from web.service import WebError

log = logging.getLogger("candy.web")

STATIC_DIR = Path(__file__).parent / "static"
GRANT_COOKIE = "candy_dl"
SESSION_COOKIE_SECURE = "__Host-candy"
SESSION_COOKIE_PLAIN = "candy"
MAX_JSON_BYTES = 64 * 1024
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; connect-src 'self'; "
       "font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra) -> None:
        super().__init__(message)
        self.status, self.extra = status, extra


class Context:
    """Everything the handlers share. Built once by build_app()."""

    def __init__(self, jobs: WebJobs, send_to_telegram=None, secure_cookie: bool | None = None,
                 trust_proxy: bool | None = None, public_url: str | None = None) -> None:
        self.jobs = jobs
        self.send_to_telegram = send_to_telegram            # async (telegram_id, path, name) -> None
        self.secure_cookie = config.WEB_COOKIE_SECURE if secure_cookie is None else secure_cookie
        self.trust_proxy = config.WEB_TRUST_PROXY if trust_proxy is None else trust_proxy
        self.public_url = config.WEB_PUBLIC_URL if public_url is None else public_url
        self.login_ip = RateLimiter(10, 300)
        self.login_user = RateLimiter(10, 900)
        self.pw_fail = RateLimiter(5, 900)                  # wrong link passwords: per link + address
        self.pw_share = RateLimiter(30, 3600)               # ... and per link overall, so many addresses can't grind
        self.grants: dict[str, tuple] = {}                  # unlocked password links: cookie value -> (id, pw_hash, until)
        self.public = RateLimiter(240, 60)                  # share-link pages and downloads, per client address
        self.heavy = RateLimiter(30, 600)                   # previews, downloads, uploads, per account
        self.streams: dict[int, int] = {}

    @property
    def cookie_name(self) -> str:
        return SESSION_COOKIE_SECURE if self.secure_cookie else SESSION_COOKIE_PLAIN


# ------------------------------------------------------------------ plumbing
def client_ip(request, ctx: Context) -> str:
    if ctx.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]       # Caddy appends the real peer last
    return request.client.host if request.client else ""


def reply(data=None, status: int = 200, **kw) -> JSONResponse:
    return JSONResponse(data if data is not None else {"ok": True}, status_code=status, **kw)


def check_origin(request, ctx: Context) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = urlparse(origin).netloc.lower()
    allowed = {request.headers.get("host", "").lower()}
    if ctx.public_url:
        allowed.add(urlparse(ctx.public_url).netloc.lower())
    if origin == "null" or host not in allowed:
        raise ApiError("Request refused (wrong origin).", 403)


async def read_json(request, limit: int = MAX_JSON_BYTES) -> dict:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ApiError("That request is too large.", 413)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise ApiError("The request wasn't valid JSON.")
    if not isinstance(data, dict):
        raise ApiError("The request must be a JSON object.")
    return data


async def read_body(request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise ApiError("That file is too large.", 413)
    return bytes(body)


def endpoint(ctx_getter, *, auth: bool = True, admin: bool = False, csrf: bool = True, allow_must_change: bool = False,
             limit: str | None = None):
    """Wrap a handler(request, ctx, account) with origin/CSRF/auth/role checks and uniform error answers."""
    def decorate(fn):
        async def handler(request):
            ctx: Context = ctx_getter(request)
            try:
                unsafe = request.method in UNSAFE
                if unsafe:
                    check_origin(request, ctx)
                account = None
                if auth:
                    token = request.cookies.get(ctx.cookie_name, "")
                    found = await asyncio.to_thread(accounts.session_account, token)
                    if found is None:
                        raise ApiError("Please sign in.", 401)
                    account, csrf_token = found
                    if unsafe and csrf and not safe_equal(request.headers.get("x-csrf-token", ""), csrf_token):
                        raise ApiError("Request refused (missing or wrong token). Reload the page.", 403)
                    if account.must_change and not allow_must_change:
                        raise ApiError("Choose a new password first.", 403, must_change=True)
                    if admin and not account.is_admin:
                        raise ApiError("Admins only.", 403)
                    if limit and not ctx.heavy.hit(f"{limit}:{account.id}"):
                        raise ApiError("Slow down a little - try again in a moment.", 429)
                request.state.token = request.cookies.get(ctx.cookie_name, "")
                return await fn(request, ctx, account)
            except ApiError as exc:
                return reply({"error": str(exc), **exc.extra}, exc.status)
            except WebError as exc:
                return reply({"error": str(exc)}, exc.status)
            except accounts.AccountError as exc:
                return reply({"error": str(exc)}, 400)
            except shares.ShareError as exc:
                return reply({"error": str(exc)}, 400)
            except SubmitError as exc:
                return reply({"error": str(exc)}, exc.status)
            except Exception:  # noqa: BLE001
                log.exception("Unhandled error in %s %s", request.method, request.url.path)
                return reply({"error": "Something went wrong on my side."}, 500)
        handler.__name__ = fn.__name__
        return handler
    return decorate


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError("Not found.", 404)


def _size_text(size: int) -> str:
    return f"{size / 1_000_000_000:.2f} GB" if size >= 1_000_000_000 else f"{size / 1_000_000:.1f} MB"


def share_page(kind: str, share=None, error: str = "") -> str:
    """The page behind a share link. Server-rendered, no script: everything is escaped."""
    import html
    if kind == "gone":
        body = '<h1>\U0001F36C Oops</h1><p class="muted">This link has expired or doesn\'t exist.</p>'
    elif kind in ("login", "owner"):
        who = "the person who made it" if kind == "owner" else "people with an account here"
        again = html.escape(f"/s/{share.token}", quote=True)
        body = (f'<h1>\U0001F512 Sign in first</h1><p class="muted">This file is only for {who}. If you are already '
                'signed in, tap Continue (your browser only shares your sign-in once you are on this site).</p>'
                f'<p><a class="btn primary" href="{again}">I\'m signed in \u2013 continue</a></p>'
                '<p><a class="btn" href="/">Sign in</a></p>')
    else:
        from urllib.parse import quote as q
        left = max(0, int(share.expires - time.time()))
        when = f"{left // 86400} days" if left >= 172800 else f"{max(1, left // 3600)} hours" if left >= 3600 else f"{max(1, left // 60)} minutes"
        link = html.escape(f"/s/{share.token}/{q(share.name)}", quote=True)
        head = (f'<h1>\U0001F36C A file for you</h1><p class="share-name">{html.escape(share.name)}</p>'
                f'<p class="muted">{_size_text(share.size)} \u00b7 link works for about {when}</p>')
        if kind == "password":
            head = '<h1>\U0001F511 A protected file</h1>'              # the name stays hidden until it is unlocked
            note = f'<p class="err">{html.escape(error)}</p>' if error else ""
            action = html.escape(f"/s/{share.token}/download", quote=True)           # no file name before the unlock
            body = (head + f'<form method="post" action="{action}" class="stack"><label class="field" for="pw">This link is '
                    'protected. Password:</label><input id="pw" name="password" type="password" autocomplete="off" required '
                    f'maxlength="100">{note}<button class="btn primary big" type="submit">Unlock</button></form>')
        else:
            body = head + f'<p><a class="btn primary big" href="{link}">Download</a></p>'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex, nofollow">'
            '<meta name="referrer" content="no-referrer"><title>Candy Downloader</title>'
            '<link rel="icon" href="/static/icon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/app.css">'
            f'</head><body class="share-body"><main class="card share-card">{body}</main></body></html>')


# ------------------------------------------------------------------ the app
def build_app(ctx: Context) -> Starlette:
    def get_ctx(_request) -> Context:
        return ctx

    def ep(**kw):
        return endpoint(get_ctx, **kw)

    # ---------------------------------------------------------- pages
    async def index(request):
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def static(request):
        name = request.path_params["name"]
        path = STATIC_DIR / name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def health(request):
        return PlainTextResponse("ok")

    # ---------------------------------------------------------- auth
    @ep(auth=False)
    async def login(request, ctx, _account):
        data = await read_json(request)
        username = str(data.get("username", "")).strip()[:64]
        password = str(data.get("password", ""))
        ip = client_ip(request, ctx)
        if ctx.login_ip.blocked(ip) or ctx.login_user.blocked(username.lower()):
            wait = max(ctx.login_ip.retry_after(ip), ctx.login_user.retry_after(username.lower()))
            raise ApiError("Too many attempts. Try again later.", 429, retry_after=wait)
        account = await asyncio.to_thread(accounts.authenticate, username, password)
        if account is None:
            ctx.login_ip.hit(ip)
            ctx.login_user.hit(username.lower())
            await asyncio.to_thread(accounts.audit, "login_failed", None, username[:40], ip)
            raise ApiError("Wrong username or password.", 401)
        token, csrf_token = await asyncio.to_thread(accounts.create_session, account.id, ip,
                                                    request.headers.get("user-agent", ""))
        await asyncio.to_thread(accounts.audit, "login", account.id, "", ip)
        response = reply({"csrf": csrf_token, "account": account.public()})
        response.set_cookie(ctx.cookie_name, token, max_age=accounts.SESSION_MAX_SECONDS, path="/", httponly=True,
                            secure=ctx.secure_cookie, samesite="strict")
        return response

    @ep()
    async def logout(request, ctx, account):
        await asyncio.to_thread(accounts.end_session, request.state.token)
        response = reply()
        response.delete_cookie(ctx.cookie_name, path="/", secure=ctx.secure_cookie, httponly=True, samesite="strict")
        return response

    @ep(allow_must_change=True)
    async def me(request, ctx, account):
        return reply({"account": account.public(), "csrf": (await asyncio.to_thread(
            accounts.session_account, request.state.token))[1],
            "owner_name": config.OWNER_NAME, "owner_emoji": config.OWNER_EMOJI,
            "sharing": sharing_info(), "warp": warp_control.configured(),
            "limits": {"max_active_jobs": ctx.jobs.max_active, "file_ttl_minutes": int(ctx.jobs.ttl // 60),
                       "quota_mb": ctx.jobs.quota // 1_000_000, "max_urls": service.MAX_URLS_PER_BATCH}})

    @ep(allow_must_change=True)
    async def change_password(request, ctx, account):
        data = await read_json(request)
        if not await asyncio.to_thread(accounts.authenticate, account.username, str(data.get("current", ""))):
            raise ApiError("The current password isn't right.", 403)
        new = str(data.get("new", ""))
        await asyncio.to_thread(accounts.set_password, account.id, new)
        # set_password signed every device out, this one included: start a fresh session
        token, csrf_token = await asyncio.to_thread(accounts.create_session, account.id, client_ip(request, ctx),
                                                    request.headers.get("user-agent", ""))
        await asyncio.to_thread(accounts.audit, "password_changed", account.id, "", client_ip(request, ctx))
        response = reply({"csrf": csrf_token})
        response.set_cookie(ctx.cookie_name, token, max_age=accounts.SESSION_MAX_SECONDS, path="/", httponly=True,
                            secure=ctx.secure_cookie, samesite="strict")
        return response

    # ---------------------------------------------------------- links, downloads, jobs
    @ep(limit="preview")
    async def preview(request, ctx, account):
        data = await read_json(request)
        return reply(await service.preview(data.get("url"), account.user_id))

    @ep(limit="submit")
    async def download(request, ctx, account):
        data = await read_json(request)
        url, settings = service.build_settings(account.user_id, data)
        rec = await ctx.jobs.submit(account, url, settings, title=str(data.get("title") or "")[:150])
        return reply(ctx.jobs.view(rec), 202)

    @ep(limit="submit")
    async def batch(request, ctx, account):
        data = await read_json(request)
        urls = data.get("urls")
        if not isinstance(urls, list) or not urls:
            raise ApiError("Pick at least one link.")
        urls = list(dict.fromkeys(service.clean_url(u) for u in urls))[:service.MAX_URLS_PER_BATCH]
        titles = data.get("titles") if isinstance(data.get("titles"), dict) else {}
        started = []
        for url in urls:
            settings = service.batch_settings(account.user_id, url, str(data.get("quality", "best")))
            try:
                rec = await ctx.jobs.submit(account, url, settings, title=str(titles.get(url) or "")[:150])
            except SubmitError as exc:
                if not started:
                    raise
                return reply({"jobs": started, "stopped": str(exc)}, 202)
            started.append(ctx.jobs.view(rec))
        return reply({"jobs": started}, 202)

    @ep()
    async def titles(request, ctx, account):
        data = await read_json(request)
        urls = [service.clean_url(u) for u in (data.get("urls") or [])][:service.MAX_URLS_PER_BATCH]
        return reply({"entries": await service.titles_for(urls, account.user_id)})

    @ep()
    async def list_jobs(request, ctx, account):
        return reply({"jobs": [ctx.jobs.view(r) for r in ctx.jobs.for_account(account.id)]})

    def owned(request, account):
        rec = ctx.jobs.get(request.path_params["rid"], account.id)
        if rec is None:
            raise ApiError("Not found.", 404)
        return rec

    @ep()
    async def cancel(request, ctx, account):
        rec = owned(request, account)
        return reply({"cancelled": ctx.jobs.cancel(rec)})

    @ep()
    async def retry(request, ctx, account):
        return reply(ctx.jobs.view(await ctx.jobs.retry(owned(request, account), account)), 202)

    @ep()
    async def dismiss(request, ctx, account):
        rec = owned(request, account)
        if not rec.outcome:
            raise ApiError("Cancel it first.", 409)
        ctx.jobs.discard(rec.rid)
        return reply()

    @ep()
    async def stream(request, ctx, account):
        """Server-sent events: the person's jobs, whenever something changed."""
        if ctx.streams.get(account.id, 0) >= 4:
            raise ApiError("Too many open tabs.", 429)

        async def events():
            ctx.streams[account.id] = ctx.streams.get(account.id, 0) + 1
            last, quiet = "", 0
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    snapshot = json.dumps({"jobs": [ctx.jobs.view(r) for r in ctx.jobs.for_account(account.id)]})
                    if snapshot != last:
                        last, quiet = snapshot, 0
                        yield f"event: jobs\ndata: {snapshot}\n\n"
                    else:
                        quiet += 1
                        if quiet >= 15:
                            quiet = 0
                            yield ": keep-alive\n\n"
                    await asyncio.sleep(1)
            finally:
                ctx.streams[account.id] = max(0, ctx.streams.get(account.id, 1) - 1)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------- files
    def file_entry(request, account):
        try:
            index = int(request.path_params["index"])
        except ValueError:
            raise ApiError("Not found.", 404)
        entry = ctx.jobs.file_for(request.path_params["rid"], account.id, index)
        if entry is None:
            raise ApiError("That file isn't available any more.", 404)
        return entry

    @ep(csrf=False)
    async def download_file(request, ctx, account):
        entry = file_entry(request, account)
        return FileResponse(entry["path"], filename=entry["name"], content_disposition_type="attachment",
                            media_type="application/octet-stream", headers={"Cache-Control": "no-store"})

    @ep(limit="submit")
    async def file_to_telegram(request, ctx, account):
        entry = file_entry(request, account)
        if account.telegram_id is None or ctx.send_to_telegram is None:
            raise ApiError("This account isn't linked to Telegram. Ask an admin to link it, or use /weblogin in the bot.")
        try:
            await ctx.send_to_telegram(account.telegram_id, entry["path"], entry["name"])
        except Exception:  # noqa: BLE001
            log.warning("Sending a web file to Telegram failed", exc_info=True)
            raise ApiError("Telegram wouldn't take it (have you started the bot?).", 502)
        return reply()

    def sharing_info() -> dict:
        cfg = shares.get_config()
        return {"available": shares.available(), "default_hours": cfg["default_hours"], "max_hours": cfg["max_hours"],
                "quota_mb": cfg["user_quota_mb"],
                "default_access": cfg["default_access"], "min_access": cfg["min_access"]}

    @ep(limit="submit")
    async def file_link(request, ctx, account):
        entry = file_entry(request, account)
        data = await read_json(request)
        if not shares.available():
            raise ApiError("Links are switched off.", 403)
        share = await asyncio.to_thread(shares.create, account.user_id, entry["path"], entry["name"],
                                        data.get("hours"), data.get("max_downloads", 0),
                                        data.get("access"), data.get("password") or "")
        return reply({"share": share.public()})

    @ep()
    async def shares_list(request, ctx, account):
        items = await asyncio.to_thread(shares.list_for, account.user_id)
        return reply({"shares": [s.public() for s in items], "used": await asyncio.to_thread(shares.usage, account.user_id),
                      **sharing_info()})

    @ep()
    async def shares_update(request, ctx, account):
        data = await read_json(request)
        allowed = {k: data[k] for k in ("access", "max_downloads", "hours", "password", "clear_password") if k in data}
        share = await asyncio.to_thread(shares.update, _int(request.path_params["id"]), account.user_id, allowed)
        if share is None:
            raise ApiError("Not found.", 404)
        return reply({"share": share.public()})

    @ep()
    async def shares_delete(request, ctx, account):
        if not await asyncio.to_thread(shares.delete, _int(request.path_params["id"]), account.user_id):
            raise ApiError("Not found.", 404)
        return reply()

    # ---------------------------------------------------------- public share links (no account needed)
    HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}

    def _page(kind: str, share=None, status: int = 200, error: str = "") -> HTMLResponse:
        return HTMLResponse(share_page(kind, share, error), status_code=status, headers=HEADERS)

    async def viewer(request):
        token = request.cookies.get(ctx.cookie_name, "")
        found = await asyncio.to_thread(accounts.session_account, token) if token else None
        return found[0] if found else None

    def granted(request, share) -> bool:
        grant = ctx.grants.get(request.cookies.get(GRANT_COOKIE, ""))
        return bool(grant and grant[0] == share.id and grant[1] == share.pw_hash and grant[2] > time.time())

    async def _gate(request):
        """(share, early response). Everything a link needs before its file may be shown or sent."""
        if not ctx.public.hit(client_ip(request, ctx)):
            return None, PlainTextResponse("Too many requests", status_code=429)
        share = await asyncio.to_thread(shares.find, request.path_params["token"])
        if share is None:
            return None, _page("gone", status=404)
        if share.access and not shares.permits(share, await viewer(request)):
            return None, _page("owner" if share.access == shares.OWNER else "login", share, status=401)
        return share, None

    async def share_landing(request):
        share, early = await _gate(request)
        if early is not None:
            return early
        if share.pw_hash and not granted(request, share):
            return _page("password", share)
        return _page("file", share)

    async def share_file(request):
        share, early = await _gate(request)
        if early is not None:
            return early
        direct = f"/s/{share.token}/{quote(share.name)}"
        if request.method == "POST":                                           # the password form
            if not share.pw_hash or granted(request, share):
                return Response(status_code=303, headers={"Location": direct})
            who, overall = f"{share.id}:{client_ip(request, ctx)}", f"{share.id}"
            if ctx.pw_fail.blocked(who) or ctx.pw_share.blocked(overall):
                return _page("password", share, 429, "Too many wrong tries. Please wait a while.")
            try:
                body = (await read_body(request, 2048)).decode("utf-8", "replace")
            except ApiError:
                return _page("password", share, 413, "That was too long.")
            password = (parse_qs(body).get("password") or [""])[0]
            if not await asyncio.to_thread(shares.check_password, share, password):
                ctx.pw_fail.hit(who)
                ctx.pw_share.hit(overall)
                return _page("password", share, 401, "That isn't the password.")
            ctx.pw_fail.reset(who)
            now = time.time()
            ctx.grants = {k: v for k, v in ctx.grants.items() if v[2] > now}
            value = secrets.token_urlsafe(24)
            ctx.grants[value] = (share.id, share.pw_hash, now + 3600)
            response = Response(status_code=303, headers={"Location": direct})
            response.set_cookie(GRANT_COOKIE, value, max_age=3600, path=f"/s/{share.token}", httponly=True,
                                samesite="strict", secure=ctx.secure_cookie)
            return response
        if share.pw_hash and not granted(request, share):
            return _page("password", share, 401)
        rng = request.headers.get("range", "")
        if request.method == "GET" and (not rng or rng.replace(" ", "").startswith("bytes=0-")):
            await asyncio.to_thread(shares.count_download, share.id)         # a resumed download isn't a new one
        return FileResponse(share.path(), filename=share.name, content_disposition_type="attachment",
                            media_type="application/octet-stream", headers=HEADERS)

    # ---------------------------------------------------------- settings, history, cookies
    @ep()
    async def get_settings_(request, ctx, account):
        return reply({"settings": service.settings_view(account.user_id), "keys": service.WEB_SETTINGS})

    @ep()
    async def put_settings(request, ctx, account):
        data = await read_json(request)
        clean = {key: service.clean_setting(key, value) for key, value in data.items()}
        for key, value in clean.items():
            await asyncio.to_thread(update_setting, account.user_id, key, value)
        return reply({"settings": service.settings_view(account.user_id)})

    @ep()
    async def reset_settings_(request, ctx, account):
        await asyncio.to_thread(reset_settings, account.user_id)
        return reply({"settings": service.settings_view(account.user_id)})

    @ep()
    async def history(request, ctx, account):
        try:
            page = max(0, int(request.query_params.get("page", "0")))
        except ValueError:
            page = 0
        size = 20
        rows = await asyncio.to_thread(ac.list_user_downloads, account.user_id, page * size, size)
        total = await asyncio.to_thread(ac.count_user_downloads, account.user_id)
        return reply({"items": rows, "total": total, "page": page, "size": size})

    @ep()
    async def clear_history(request, ctx, account):
        return reply({"deleted": await asyncio.to_thread(ac.clear_user_downloads, account.user_id)})

    @ep()
    async def cookies_list(request, ctx, account):
        return reply({"sites": await asyncio.to_thread(service.cookie_sites, account.user_id)})

    @ep(limit="upload")
    async def cookies_add(request, ctx, account):
        data = await read_body(request, service.MAX_COOKIE_BYTES)
        result = await asyncio.to_thread(service.save_cookies, account.user_id, data)
        return reply({**result, "sites": await asyncio.to_thread(service.cookie_sites, account.user_id)})

    @ep()
    async def cookies_remove(request, ctx, account):
        site = request.path_params["site"]
        if not re.fullmatch(r"[a-z0-9.-]{1,80}", site):
            raise ApiError("Unknown site.", 404)
        await asyncio.to_thread(service.remove_cookie_site, account.user_id, site)
        return reply({"sites": await asyncio.to_thread(service.cookie_sites, account.user_id)})

    # ---------------------------------------------------------- toolbox
    @ep()
    async def tools_list(request, ctx, account):
        items = []
        for rid in tools.store.for_user(account.user_id):
            item = tools.store.get(rid, account.user_id)
            if item is not None:
                items.append(service.tool_view(rid, item))
        return reply({"items": items})

    @ep(limit="upload")
    async def tools_upload(request, ctx, account):
        name = re.sub(r"[\x00-\x1f/\\]", "", request.query_params.get("name", "file"))[:200] or "file"
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > service.MAX_UPLOAD_BYTES:
            raise ApiError("That file is bigger than the 2 GB limit.", 413)
        rid = secrets.token_hex(8)
        item = await service.receive_upload(request.stream(), account.user_id, name, rid)
        return reply(service.tool_view(rid, item), 201)

    def tool_item(request, account):
        item = tools.store.get(request.path_params["rid"], account.user_id)
        if item is None:
            raise ApiError("That file expired - upload it again.", 404)
        return item

    @ep(limit="upload")
    async def tools_srt(request, ctx, account):
        rid = request.path_params["rid"]
        data = await read_body(request, 2_000_000)
        await asyncio.to_thread(service.save_srt, account.user_id, rid, data)
        return reply(service.tool_view(rid, tool_item(request, account)))

    @ep(limit="submit")
    async def tools_run(request, ctx, account):
        item = tool_item(request, account)
        rid = request.path_params["rid"]
        data = await read_json(request)
        tool = str(data.get("tool", ""))
        params = service.tool_params(item, tool, data)
        if ctx.jobs.manager.is_active(rid):
            raise ApiError("Already working on this file - wait for it to finish.", 409)
        from settings.user_settings import get_settings
        base = dict(get_settings(account.user_id))
        settings = tools.tool_settings(base, item, tool, **params)
        # a toolbox job keeps the upload's id so the file stays available for the next tool
        for old in [r for r in ctx.jobs.for_account(account.id) if r.rid == rid]:
            if old.outcome:
                ctx.jobs.discard(old.rid)
        rec = await ctx.jobs.submit(account, f"tool://{tool}", settings, title=item.name, kind="tool", rid=rid)
        return reply(ctx.jobs.view(rec), 202)

    @ep()
    async def tools_remove(request, ctx, account):
        tool_item(request, account)
        tools.store.discard(request.path_params["rid"])
        return reply()

    # ---------------------------------------------------------- admin
    @ep(admin=True)
    async def admin_accounts(request, ctx, account):
        return reply({"accounts": [a.public() for a in await asyncio.to_thread(accounts.list_accounts)]})

    @ep(admin=True)
    async def admin_create(request, ctx, account):
        data = await read_json(request)
        password = str(data.get("password") or "") or generate_password()
        telegram_id = data.get("telegram_id")
        if telegram_id not in (None, ""):
            if isinstance(telegram_id, bool) or not str(telegram_id).lstrip("-").isdigit() or int(telegram_id) <= 0:
                raise ApiError("A Telegram id is a positive number.")
            telegram_id = int(telegram_id)
        else:
            telegram_id = None
        created = await asyncio.to_thread(accounts.create_account, str(data.get("username", "")), password,
                                          str(data.get("role", "user")), telegram_id, True)
        await asyncio.to_thread(accounts.audit, "account_created", account.id, created.username, client_ip(request, ctx))
        return reply({"account": created.public(), "password": password}, 201)

    def target_id(request) -> int:
        try:
            return int(request.path_params["id"])
        except ValueError:
            raise ApiError("Not found.", 404)

    @ep(admin=True)
    async def admin_reset(request, ctx, account):
        target = await asyncio.to_thread(accounts.get_account, target_id(request))
        if target is None:
            raise ApiError("Not found.", 404)
        password = generate_password()
        await asyncio.to_thread(accounts.set_password, target.id, password, True)
        await asyncio.to_thread(accounts.audit, "password_reset", account.id, target.username, client_ip(request, ctx))
        return reply({"password": password})

    @ep(admin=True)
    async def admin_update(request, ctx, account):
        target = await asyncio.to_thread(accounts.get_account, target_id(request))
        if target is None:
            raise ApiError("Not found.", 404)
        data = await read_json(request)
        if "role" in data:
            await asyncio.to_thread(accounts.set_role, target.id, str(data["role"]))
        if "disabled" in data:
            if target.id == account.id and data["disabled"]:
                raise ApiError("You can't disable your own account.")
            await asyncio.to_thread(accounts.set_disabled, target.id, bool(data["disabled"]))
            if data["disabled"]:
                ctx.jobs.discard_account(target.id)
        await asyncio.to_thread(accounts.audit, "account_updated", account.id, f"{target.username} {sorted(data)}",
                                client_ip(request, ctx))
        return reply({"account": (await asyncio.to_thread(accounts.get_account, target.id)).public()})

    @ep(admin=True)
    async def admin_delete(request, ctx, account):
        target = await asyncio.to_thread(accounts.get_account, target_id(request))
        if target is None:
            raise ApiError("Not found.", 404)
        if target.id == account.id:
            raise ApiError("You can't delete your own account.")
        ctx.jobs.discard_account(target.id)
        if target.telegram_id is None:                      # a Telegram-linked person keeps their links via the bot
            await asyncio.to_thread(shares.delete_owner, target.user_id)
        await asyncio.to_thread(accounts.delete_account, target.id)
        await asyncio.to_thread(accounts.audit, "account_deleted", account.id, target.username, client_ip(request, ctx))
        return reply()

    @ep(admin=True)
    async def admin_sharing_get(request, ctx, account):
        items = await asyncio.to_thread(shares.list_for, None)
        return reply({**shares.get_config(), "count": len(items), "bytes": sum(i.size for i in items),
                      "public_url": shares.base_url(), "web_enabled": bool(config.WEB_ENABLED),
                      "shares": [{**i.public(), "owner": i.owner} for i in items[:200]]})

    @ep(admin=True)
    async def admin_sharing_put(request, ctx, account):
        data = await read_json(request)
        result = await asyncio.to_thread(shares.set_config, data)
        await asyncio.to_thread(accounts.audit, "sharing_changed", account.id, "", client_ip(request, ctx))
        return reply(result)

    @ep(admin=True)
    async def admin_share_delete(request, ctx, account):
        if not await asyncio.to_thread(shares.delete, _int(request.path_params["id"])):
            raise ApiError("Not found.", 404)
        await asyncio.to_thread(accounts.audit, "share_deleted", account.id, request.path_params["id"], client_ip(request, ctx))
        return reply()

    @ep(admin=True)
    async def admin_overview(request, ctx, account):
        total, ok, failed = await asyncio.to_thread(ac.download_stats)
        return reply({"queue": ctx.jobs.manager.active_count(), "downloads": {"total": total, "ok": ok, "failed": failed},
                      "bot_users": await asyncio.to_thread(ac.known_user_count),
                      "accounts": len(await asyncio.to_thread(accounts.list_accounts)),
                      "access_mode": await asyncio.to_thread(ac.get_mode),
                      "allowed_users": await asyncio.to_thread(ac.list_allowed_users),
                      "warp": {"configured": warp_control.configured(), "cooldown": warp_control.cooldown_left()}})

    @ep(admin=True)
    async def admin_access(request, ctx, account):
        data = await read_json(request)
        if "mode" in data:
            if data["mode"] not in ("public", "private"):
                raise ApiError("Mode is public or private.")
            await asyncio.to_thread(ac.set_mode, data["mode"])
        for key, fn in (("allow", ac.add_allowed_user), ("remove", ac.remove_allowed_user)):
            if key in data:
                value = data[key]
                if isinstance(value, bool) or not str(value).isdigit():
                    raise ApiError("A Telegram id is a positive number.")
                await asyncio.to_thread(fn, int(value))
        await asyncio.to_thread(accounts.audit, "access_changed", account.id, str(sorted(data)), client_ip(request, ctx))
        return reply({"access_mode": ac.get_mode(), "allowed_users": ac.list_allowed_users()})

    @ep(admin=True)
    async def admin_warp(request, ctx, account):
        return reply({"configured": warp_control.configured(), "ip": await warp_control.current_ip(
            warp_control.proxy_url()) if warp_control.configured() else None,
            "cooldown": warp_control.cooldown_left(), "busy": warp_control.busy()})

    @ep(admin=True)
    async def admin_warp_rotate(request, ctx, account):
        result = await warp_control.rotate()
        await asyncio.to_thread(accounts.audit, "warp_rotate", account.id, result.message[:100], client_ip(request, ctx))
        return reply({"ok": result.ok, "message": result.message, "old_ip": result.old_ip, "new_ip": result.new_ip})

    @ep(admin=True)
    async def admin_audit(request, ctx, account):
        return reply({"items": await asyncio.to_thread(accounts.recent_audit, 100)})

    routes = [
        Route("/", index), Route("/healthz", health), Route("/static/{name}", static),
        Route("/s/{token}", share_landing), Route("/s/{token}/{name:path}", share_file, methods=["GET", "HEAD", "POST"]),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
        Route("/api/me", me), Route("/api/me/password", change_password, methods=["POST"]),
        Route("/api/preview", preview, methods=["POST"]),
        Route("/api/download", download, methods=["POST"]),
        Route("/api/batch", batch, methods=["POST"]),
        Route("/api/titles", titles, methods=["POST"]),
        Route("/api/jobs", list_jobs),
        Route("/api/jobs/stream", stream),
        Route("/api/jobs/{rid}/cancel", cancel, methods=["POST"]),
        Route("/api/jobs/{rid}/retry", retry, methods=["POST"]),
        Route("/api/jobs/{rid}/dismiss", dismiss, methods=["POST"]),
        Route("/api/files/{rid}/{index}", download_file),
        Route("/api/files/{rid}/{index}/telegram", file_to_telegram, methods=["POST"]),
        Route("/api/files/{rid}/{index}/link", file_link, methods=["POST"]),
        Route("/api/settings", get_settings_), Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/settings/reset", reset_settings_, methods=["POST"]),
        Route("/api/history", history), Route("/api/history/clear", clear_history, methods=["POST"]),
        Route("/api/cookies", cookies_list), Route("/api/cookies", cookies_add, methods=["POST"]),
        Route("/api/cookies/{site}", cookies_remove, methods=["DELETE"]),
        Route("/api/shares", shares_list), Route("/api/shares/{id}", shares_delete, methods=["DELETE"]),
        Route("/api/shares/{id}", shares_update, methods=["PATCH"]),
        Route("/api/tools", tools_list), Route("/api/tools/upload", tools_upload, methods=["PUT"]),
        Route("/api/tools/{rid}/srt", tools_srt, methods=["PUT"]),
        Route("/api/tools/{rid}/run", tools_run, methods=["POST"]),
        Route("/api/tools/{rid}", tools_remove, methods=["DELETE"]),
        Route("/api/admin/accounts", admin_accounts), Route("/api/admin/accounts", admin_create, methods=["POST"]),
        Route("/api/admin/accounts/{id}", admin_update, methods=["PATCH"]),
        Route("/api/admin/accounts/{id}", admin_delete, methods=["DELETE"]),
        Route("/api/admin/accounts/{id}/reset", admin_reset, methods=["POST"]),
        Route("/api/admin/sharing", admin_sharing_get), Route("/api/admin/sharing", admin_sharing_put, methods=["PUT"]),
        Route("/api/admin/shares/{id}", admin_share_delete, methods=["DELETE"]),
        Route("/api/admin/overview", admin_overview), Route("/api/admin/access", admin_access, methods=["POST"]),
        Route("/api/admin/warp", admin_warp), Route("/api/admin/warp/rotate", admin_warp_rotate, methods=["POST"]),
        Route("/api/admin/audit", admin_audit),
    ]
    app = Starlette(routes=routes)
    return SecurityHeaders(app)


class SecurityHeaders:
    """Adds the protective headers to every response (pure ASGI, so streaming answers are untouched)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"server",)]
                names = {k.lower() for k, _ in headers}
                extra = {b"content-security-policy": CSP, b"x-content-type-options": b"nosniff",
                         b"x-frame-options": b"DENY", b"referrer-policy": b"no-referrer",
                         b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                         b"cross-origin-opener-policy": b"same-origin"}
                if scope["path"].startswith("/api/") and b"cache-control" not in names:
                    extra[b"cache-control"] = b"no-store"
                for key, value in extra.items():
                    if key not in names:
                        headers.append((key, value.encode() if isinstance(value, str) else value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)
