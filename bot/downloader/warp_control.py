"""
Ask the WARP control service for a fresh WARP address, and check what address we really leave from.

  current_ip(proxy)   the public address seen through the proxy (Cloudflare's own /cdn-cgi/trace), or None
  rotate()            re-register + restart WARP, wait until it is back, report old -> new address

Safety rails: one change at a time, a cooldown between changes (WARP_ROTATE_COOLDOWN_SECONDS), and the proxy
policy forgets "blocked" marks afterwards because it is a different address now.
"""
import asyncio
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

import config
from downloader import proxy as proxy_module

log = logging.getLogger("candy.warp")

_lock = asyncio.Lock()
_last_change = 0.0


@dataclass
class RotateResult:
    ok: bool
    message: str
    old_ip: str | None = None
    new_ip: str | None = None


def configured() -> bool:
    return bool(config.WARP_CONTROL_URL and config.WARP_CONTROL_TOKEN)


def proxy_url() -> str | None:
    return config.YT_PROXIES[0] if config.YT_PROXIES else None


def _fetch_ip(proxy: str) -> str | None:
    """Blocking: GET https://www.cloudflare.com/cdn-cgi/trace through a SOCKS proxy and read `ip=`."""
    import socks                                      # PySocks (already required by gallery-dl routing)
    parts = urlparse(proxy)
    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, parts.hostname, parts.port or 1080, rdns=True)
    sock.settimeout(10)
    try:
        sock.connect(("www.cloudflare.com", 443))
        with ssl.create_default_context().wrap_socket(sock, server_hostname="www.cloudflare.com") as tls:
            tls.sendall(b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: www.cloudflare.com\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := tls.recv(4096):
                data += chunk
    except OSError:
        return None
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    return None


async def current_ip(proxy: str | None = None) -> str | None:
    proxy = proxy or proxy_url()
    if not proxy:
        return None
    return await asyncio.to_thread(_fetch_ip, proxy)


def _post_rotate() -> dict:
    request = urllib.request.Request(f"{config.WARP_CONTROL_URL}/rotate", method="POST",
                                     headers={"Authorization": f"Bearer {config.WARP_CONTROL_TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except (ValueError, OSError):
            return {"ok": False, "detail": f"control service answered {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "detail": f"couldn't reach the control service ({type(exc).__name__})"}


def cooldown_left(now: float | None = None) -> int:
    left = _last_change + config.WARP_ROTATE_COOLDOWN_SECONDS - (time.time() if now is None else now)
    return max(0, int(left + 0.999))


def busy() -> bool:
    return _lock.locked()


async def rotate() -> RotateResult:
    global _last_change
    if not configured():
        return RotateResult(False, "The WARP control service isn't set up (see WARP_CONTROL_URL).")
    left = cooldown_left()
    if left:
        return RotateResult(False, f"Changed a moment ago - wait {left} s before changing again.")
    if _lock.locked():
        return RotateResult(False, "A change is already running.")
    async with _lock:
        proxy = proxy_url()
        old_ip = await current_ip(proxy)
        _last_change = time.time()                    # counts even when it fails: no hammering
        answer = await asyncio.to_thread(_post_rotate)
        if not answer.get("ok"):
            return RotateResult(False, f"WARP wasn't changed: {answer.get('detail', 'unknown error')}", old_ip)
        new_ip = None
        for _ in range(10):                           # the tunnel needs a few seconds after it reports "connected"
            new_ip = await current_ip(proxy)
            if new_ip:
                break
            await asyncio.sleep(3)
        proxy_module.policy.reset_proxy(proxy)
        if not new_ip:
            return RotateResult(False, "WARP restarted but isn't answering yet - try again in a minute.", old_ip)
        if new_ip == old_ip:
            return RotateResult(True, "WARP restarted but kept the same address (Cloudflare handed back the same one).",
                                old_ip, new_ip)
        return RotateResult(True, "WARP has a new address.", old_ip, new_ip)
