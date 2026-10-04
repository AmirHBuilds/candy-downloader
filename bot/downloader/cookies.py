"""
Cookie handling shared by the preview probe and the actual downloader.

Before this existed, only the downloader knew about a user's cookies. The
preview probe (the "checking link" step that fetches the title/qualities)
never received a user id, so it always ran logged-out - which is why a
freshly uploaded cookies.txt "did nothing": the preview kept demanding a
login no matter what.
"""
import time
from pathlib import Path

from config import COOKIES_DIR

# With logged-in cookies, yt-dlp's default client for YouTube is currently
# broken on YouTube's side ("The page needs to be reloaded", playability
# UNPLAYABLE - yt-dlp issue #17389). The maintainers' documented
# workaround is to keep the default clients but add web_embedded.
YOUTUBE_COOKIE_CLIENTS = ["default", "web_embedded"]


def cookie_file_for(user_id: int, settings: dict) -> str | None:
    """Path to this user's cookies.txt if they have one AND it's switched on."""
    if not settings.get("cookies_enabled"):
        return None
    path = Path(COOKIES_DIR) / f"{user_id}.txt"
    return str(path) if path.exists() else None


def apply_cookies(opts: dict, cookie_path: str) -> None:
    """Point yt-dlp at the cookie file and use YouTube clients that still
    work for logged-in sessions. Merges into any extractor_args already set
    (e.g. the PO-token provider URL) instead of replacing them."""
    opts["cookiefile"] = cookie_path
    extractor_args = opts.setdefault("extractor_args", {})
    youtube_args = dict(extractor_args.get("youtube", {}))
    youtube_args["player_client"] = list(YOUTUBE_COOKIE_CLIENTS)
    extractor_args["youtube"] = youtube_args


_LOGIN_COOKIE_NAMES = {"SAPISID", "__Secure-3PSID", "__Secure-1PSID", "LOGIN_INFO", "SID"}


def inspect_cookie_file(path: str | Path) -> dict:
    """Cheap sanity check of an uploaded cookies file, so the person finds
    out immediately if it can't possibly work rather than after a failed
    download. Returns {"netscape", "youtube", "logged_in", "expired"} (all bool);
    "expired" = there are login cookies and every one of them is already past
    its expiry date (session cookies, which carry none, never count as expired)."""
    result = {"netscape": False, "youtube": False, "logged_in": False, "expired": False}
    login_expiries: list[int] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return result

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if "Netscape" in line:
                result["netscape"] = True
            if not line.startswith("#HttpOnly_"):
                continue
            line = line[len("#HttpOnly_"):]  # HttpOnly cookies are written with this prefix
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, name = fields[0], fields[5]
        if "youtube.com" in domain or "google.com" in domain:
            result["youtube"] = True
            if name in _LOGIN_COOKIE_NAMES:
                result["logged_in"] = True
                try:
                    login_expiries.append(int(float(fields[4])))   # 0 = a session cookie
                except ValueError:
                    login_expiries.append(0)
    if login_expiries and all(0 < expiry < time.time() for expiry in login_expiries):
        result["expired"] = True
    return result
