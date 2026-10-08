"""
When a site blocks this server's IP address, route THAT site - and only that site - through a proxy
(the bundled Cloudflare WARP container), without paying for it the rest of the time.

Datacenter addresses get "Sign in to confirm you're not a bot" (YouTube), 403s and 429s (X, Instagram,
Reddit, ...) however good the cookies are, and a cookie can't fix an IP block. So:

  auto   (default)  go DIRECT. The first time a site answers with a block-type error, remember
                    "direct is blocked for THIS site" for DIRECT_BLOCK_SECONDS and use the proxy for it;
                    after that try direct again. Other sites are unaffected, and a home connection never
                    touches the proxy at all.
  always            use the proxy whenever it is reachable (for the listed sites).
  off               never.

Which sites may use the proxy is config.PROXY_DOMAINS (default: YouTube, X, Instagram, Pinterest, Reddit,
TikTok; "*" = all). A proxy that is not running is skipped (a quick connection test), so the bot keeps
working exactly as before if the WARP container is down. A proxy that itself gets blocked for a site is set
aside for that site for a while and the next one - or direct - is tried. Someone's own proxy
(Settings > Advanced) always wins and bypasses all of this.

Note: only yt-dlp and gallery-dl are routed. aria2c (direct-file downloads) cannot speak SOCKS.
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

# What sites answer when they refuse the address (not the video, not the cookies). yt-dlp words these
# "HTTP Error 403"; gallery-dl says "403 Forbidden"; Instagram's own rate limit has its own phrase.
_BLOCK_MARKERS = ("sign in to confirm", "not a bot", "http error 403", "http error 429", "too many requests",
                  "403 forbidden", "rate-limit reached", "rate limit reached")

# Different hostnames of one site share one "is direct blocked?" record.
_SITE_ALIASES = {"youtu.be": "youtube.com", "twitter.com": "x.com", "pin.it": "pinterest.com"}
_SITE_LABELS = {"youtube.com": "YouTube", "x.com": "X", "instagram.com": "Instagram",
                "pinterest.com": "Pinterest", "reddit.com": "Reddit", "tiktok.com": "TikTok"}


def is_ip_block(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def is_youtube(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("youtu.be", "youtube.com") or host.endswith(".youtube.com")


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def site_for(url: str, domains=None) -> str | None:
    """The site key this link belongs to if it may use the proxy ("youtube.com", "x.com", ...), else None.
    A domain matches itself and its subdomains - never a mere substring (dropbox.com is not x.com)."""
    domains = config.PROXY_DOMAINS if domains is None else domains
    host = _host(url)
    if not host:
        return None
    if "*" in domains:
        return _SITE_ALIASES.get(host, host)
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return _SITE_ALIASES.get(domain, domain)
    return None


def site_label(url: str) -> str:
    """A name for messages: "YouTube", "X", or the plain hostname."""
    host = _host(url)
    for domain in list(_SITE_LABELS) + list(_SITE_ALIASES):
        if host == domain or host.endswith("." + domain):
            return _SITE_LABELS.get(_SITE_ALIASES.get(domain, domain), domain)
    return (host[4:] if host.startswith("www.") else host) or "This site"


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
                 direct_block_seconds: float = DIRECT_BLOCK_SECONDS, proxy_block_seconds: float = PROXY_BLOCK_SECONDS,
                 domains=None):
        self.proxies = list(proxies)
        self.mode = mode if mode in ("auto", "always", "off") else "auto"
        self.domains = list(config.PROXY_DOMAINS if domains is None else domains)
        self._clock, self._alive = clock, alive
        self._direct_block = direct_block_seconds
        self._proxy_block = proxy_block_seconds
        self._direct_blocked_until: dict[str, float] = {}          # site -> until
        self._blocked_until: dict[tuple[str, str], float] = {}     # (site, proxy) -> until
        self._alive_cache: dict[str, tuple[float, bool]] = {}
        self._last_good: dict[str, str] = {}                       # site -> proxy

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.proxies)

    def _site(self, url: str) -> str | None:
        return site_for(url, self.domains) if self.enabled else None

    def _is_alive(self, proxy: str) -> bool:
        now = self._clock()
        cached = self._alive_cache.get(proxy)
        if cached and now - cached[0] < ALIVE_CACHE_SECONDS:
            return cached[1]
        result = self._alive(proxy)
        self._alive_cache[proxy] = (now, result)
        return result

    def _usable(self, site: str) -> list[str]:
        now = self._clock()
        ready = [p for p in self.proxies if self._blocked_until.get((site, p), 0) <= now and self._is_alive(p)]
        good = self._last_good.get(site)
        if good in ready:                                  # stick with what worked last for this site
            ready.remove(good)
            ready.insert(0, good)
        return ready

    def route(self, url: str) -> str | None:
        """The proxy to use for this request, or None for direct."""
        site = self._site(url)
        if site is None:
            return None
        usable = self._usable(site)
        if not usable:
            return None
        if self.mode == "always" or self._direct_blocked_until.get(site, 0) > self._clock():
            return usable[0]
        return None

    def report_failure(self, url: str, proxy: str | None, error_text: str) -> bool:
        """A request failed. True = the route has changed, so trying once more is worthwhile."""
        site = self._site(url)
        if site is None or not is_ip_block(error_text):
            return False
        now = self._clock()
        if proxy is None:
            if not self._usable(site):
                return False                               # nowhere else to go
            self._direct_blocked_until[site] = now + self._direct_block
            log.info("%s is blocking this server's address; using the proxy for it for the next %d minutes",
                     site, self._direct_block // 60)
        else:
            self._blocked_until[(site, proxy)] = now + self._proxy_block
            if self._last_good.get(site) == proxy:
                del self._last_good[site]
            log.info("The proxy %s was blocked by %s too; setting it aside for it for %d minutes", _hide(proxy),
                     site, self._proxy_block // 60)
        return self.route(url) != proxy

    def report_success(self, url: str, proxy: str | None) -> None:
        site = self._site(url)
        if site is None:
            return
        if proxy is None:
            self._direct_blocked_until.pop(site, None)     # direct works again
        else:
            self._last_good[site] = proxy


def _hide(proxy: str) -> str:
    parsed = urlparse(proxy)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


policy = ProxyPolicy(config.YT_PROXIES, config.YT_PROXY_MODE)
