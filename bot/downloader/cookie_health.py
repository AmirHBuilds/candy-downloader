"""
Tells a person when their YouTube cookies have stopped working, instead of
every download just failing with a vague sign-in error.

YouTube rotates login sessions, so an exported cookies.txt dies after a while
and the file itself still LOOKS fine (its expiry dates are far in the future),
so the only reliable signal is what happens when it is used. The tracker
counts consecutive sign-in failures for people whose cookies were in play and
raises ONE alert - after a second failure, or immediately when yt-dlp says
outright that the cookies are no longer valid - then stays quiet for a while.

A sign-in failure can also mean YouTube is blocking the server's IP, which no
cookie fixes, so the alert says so rather than blaming the cookies alone.
"""
import time
from urllib.parse import urlparse

from downloader.cookies import cookie_file_for

# yt-dlp: "The provided YouTube account cookies are no longer valid..." - unambiguous.
_EXPLICIT = ("cookies are no longer valid",)
# "Sign in to confirm you're not a bot" / "...to confirm your age" - dead cookies OR a blocked IP.
_LOGIN = ("sign in to confirm", "not a bot")

COOKIE_ALERT = (
    "🔑 <b>Your YouTube cookies may have stopped working</b>\n\n"
    "YouTube rotates login sessions, so exported cookies expire after a while "
    "(or it may just be blocking this server's connection).\n\n"
    "Export fresh ones from a private window, close it right away, and send the file again. "
    "/cookies has the steps."
)


def classify(error_text: str) -> str | None:
    """'dead' (yt-dlp says so), 'login' (a sign-in demand), or None (unrelated)."""
    lowered = (error_text or "").lower()
    if any(marker in lowered for marker in _EXPLICIT):
        return "dead"
    if any(marker in lowered for marker in _LOGIN):
        return "login"
    return None


def cookies_in_use(user_id: int, settings: dict, url: str) -> bool:
    """Were this person's cookies actually part of a request for this link?
    Only YouTube cares, and only when they have a file and have it enabled."""
    host = (urlparse(url).hostname or "").lower()
    if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
        return False
    return cookie_file_for(user_id, settings) is not None


class CookieWatch:
    def __init__(self, clock=time.time, threshold: int = 2, cooldown_seconds: float = 12 * 3600):
        self._clock = clock
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._streak: dict[int, int] = {}
        self._last_alert: dict[int, float] = {}

    def record_failure(self, user_id: int, error_text: str) -> bool:
        """True when the person should be warned now. Errors unrelated to
        signing in are ignored entirely: they neither count nor reset."""
        kind = classify(error_text)
        if kind is None:
            return False
        self._streak[user_id] = self._streak.get(user_id, 0) + 1
        if kind != "dead" and self._streak[user_id] < self._threshold:
            return False
        now = self._clock()
        if now - self._last_alert.get(user_id, float("-inf")) < self._cooldown:
            return False
        self._last_alert[user_id] = now
        return True

    def record_success(self, user_id: int) -> None:
        """A request with their cookies worked - whatever went wrong before is over."""
        self._streak.pop(user_id, None)


watch = CookieWatch()
