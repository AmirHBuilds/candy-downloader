"""
When YouTube blocks this server's IP address, route YouTube - and only YouTube - through a proxy
(the bundled Cloudflare WARP container), without paying for it the rest of the time.

Datacenter addresses get "Sign in to confirm you're not a bot" however good the cookies are, and a
cookie can't fix an IP block. So:

  auto   (default)  go DIRECT. The first time YouTube answers with a block-type error, remember
                    "direct is blocked" for DIRECT_BLOCK_SECONDS and use the proxy; after that
                    try direct again. A home connection never touches the proxy at all.
  always            use the proxy whenever it is reachable.
  off               never.

A proxy that is not running is skipped (a quick connection test), so the bot keeps working exactly
as before if the WARP container is down. A proxy that itself gets blocked is set aside for a while
and the next one - or direct - is tried. Someone's own proxy (Settings > Advanced) always wins and
bypasses all of this.
"""
import logging
import socket
import time
from urllib.parse import urlparse

import config

log = logging.getLogger("candy.proxy")

DIRECT_BLOCK_SECONDS = 30 * 60
PROXY_BLOCK_SECONDS = 10 * 60
ALIVE_CACHE_SECONDS = 15

# What YouTube answers when it refuses the address (not the video, not the cookies).
_BLOCK_MARKERS = ("sign in to confirm", "not a bot", "http error 403", "http error 429", "too many requests")


def is_ip_block(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def is_youtube(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com")


def tcp_alive(proxy: str) -> bool:
    """Is anything listening at the proxy's host:port? (Not a test that it works - that is what the
    first real request finds out.)"""
    parsed = urlparse(proxy)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 1080), timeout=0.5):
            return True
    except (OSError, TypeError, ValueError):
        return False


class ProxyPolicy:
    def __init__(self, proxies: list[str], mode: str = "auto", clock=time.time, alive=tcp_alive,
                 direct_block_seconds: float = DIRECT_BLOCK_SECONDS, proxy_block_seconds: float = PROXY_BLOCK_SECONDS):
        self.proxies = list(proxies)
        self.mode = mode if mode in ("auto", "always", "off") else "auto"
        self._clock, self._alive = clock, alive
        self._direct_block = direct_block_seconds
        self._proxy_block = proxy_block_seconds
        self._direct_blocked_until = 0.0
        self._blocked_until: dict[str, float] = {}
        self._alive_cache: dict[str, tuple[float, bool]] = {}
        self._last_good: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.proxies)

    def _is_alive(self, proxy: str) -> bool:
        now = self._clock()
        cached = self._alive_cache.get(proxy)
        if cached and now - cached[0] < ALIVE_CACHE_SECONDS:
            return cached[1]
        result = self._alive(proxy)
        self._alive_cache[proxy] = (now, result)
        return result

    def _usable(self) -> list[str]:
        now = self._clock()
        ready = [p for p in self.proxies if self._blocked_until.get(p, 0) <= now and self._is_alive(p)]
        if self._last_good in ready:                       # stick with what worked last
            ready.remove(self._last_good)
            ready.insert(0, self._last_good)
        return ready

    def route(self, url: str) -> str | None:
        """The proxy to use for this request, or None for direct."""
        if not self.enabled or not is_youtube(url):
            return None
        usable = self._usable()
        if not usable:
            return None
        if self.mode == "always" or self._direct_blocked_until > self._clock():
            return usable[0]
        return None

    def report_failure(self, url: str, proxy: str | None, error_text: str) -> bool:
        """A YouTube request failed. True = the route has changed, so trying once more is worthwhile."""
        if not self.enabled or not is_youtube(url) or not is_ip_block(error_text):
            return False
        now = self._clock()
        if proxy is None:
            if not self._usable():
                return False                               # nowhere else to go
            self._direct_blocked_until = now + self._direct_block
            log.info("YouTube is blocking this server's address; using the proxy for the next %d minutes",
                     self._direct_block // 60)
        else:
            self._blocked_until[proxy] = now + self._proxy_block
            if self._last_good == proxy:
                self._last_good = None
            log.info("The proxy %s was blocked too; setting it aside for %d minutes", _hide(proxy),
                     self._proxy_block // 60)
        return self.route(url) != proxy

    def report_success(self, url: str, proxy: str | None) -> None:
        if not is_youtube(url):
            return
        if proxy is None:
            self._direct_blocked_until = 0.0               # direct works again
        else:
            self._last_good = proxy


def _hide(proxy: str) -> str:
    parsed = urlparse(proxy)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


policy = ProxyPolicy(config.YT_PROXIES, config.YT_PROXY_MODE)
