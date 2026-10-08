"""
Cookie handling shared by the preview probe and the actual downloader.

Before this existed, only the downloader knew about a user's cookies. The
preview probe (the "checking link" step that fetches the title/qualities)
never received a user id, so it always ran logged-out - which is why a
freshly uploaded cookies.txt "did nothing": the preview kept demanding a
login no matter what.
"""
import os
import re
import time
from dataclasses import dataclass
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


# ---------------------------------------------------------------- one file, one block per site
# A person has ONE cookies file (cookies/<user_id>.txt - every consumer reads that), but uploading cookies for one
# site must not wipe another's. So an upload REPLACES only the sites it contains and keeps the rest.

_SECOND_LEVEL = {"co.uk", "org.uk", "com.au", "co.jp", "com.br", "co.in", "com.tr", "com.mx", "co.kr", "com.cn"}
_SITE_ALIASES = {"twitter.com": "x.com", "youtu.be": "youtube.com", "googlevideo.com": "youtube.com",
                 "youtube-nocookie.com": "youtube.com", "ytimg.com": "youtube.com"}
SITE_LABELS = {"youtube.com": "YouTube", "x.com": "X", "instagram.com": "Instagram", "pinterest.com": "Pinterest",
               "reddit.com": "Reddit", "tiktok.com": "TikTok", "facebook.com": "Facebook"}
# One cookie whose presence proves a login (so "exported while logged out" can be said out loud).
_LOGIN_COOKIE = {"instagram.com": "sessionid", "x.com": "auth_token"}


def site_of(domain: str) -> str:
    """The site a cookie belongs to: "accounts.google.com" and ".youtube.com" are both YouTube (one login),
    "twitter.com" is X, anything else is its last two labels."""
    host = domain.strip().lower().lstrip(".")
    if host.startswith("#httponly_"):
        host = host[len("#httponly_"):].lstrip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        key = host
    elif ".".join(parts[-2:]) in _SECOND_LEVEL:
        key = ".".join(parts[-3:])
    else:
        key = ".".join(parts[-2:])
    if key.split(".")[0] == "google":                    # google.com, google.de, google.co.uk (+ accounts.google.com)
        return "youtube.com"
    return _SITE_ALIASES.get(key, key)


def site_label(site: str) -> str:
    return SITE_LABELS.get(site, site)


@dataclass
class CookieEntry:
    site: str
    raw: str
    name: str
    expiry: int


def parse_entries(text: str) -> list[CookieEntry]:
    """Every cookie line of a Netscape file (HttpOnly ones included), kept exactly as written."""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        body = line
        if line.startswith("#"):
            if not line.startswith("#HttpOnly_"):
                continue
            body = line[len("#HttpOnly_"):]
        fields = body.split("\t")
        if len(fields) < 7:
            continue
        try:
            expiry = int(float(fields[4]))
        except ValueError:
            expiry = 0
        entries.append(CookieEntry(site_of(fields[0]), line, fields[5], expiry))
    return entries


def _read(path: Path) -> list[CookieEntry]:
    try:
        return parse_entries(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []


def _write(path: Path, entries: list[CookieEntry]) -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    lines = ["# Netscape HTTP Cookie File", "# One block per site; uploading a site again replaces just that site."]
    lines += [entry.raw for entry in entries]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@dataclass
class MergeResult:
    added: list[str]          # sites that are new
    replaced: list[str]       # sites that had cookies and now have the uploaded ones
    kept: list[str]           # sites the upload did not touch
    no_login: list[str]       # sites (with a known login cookie) where the upload has none


def merge_upload(existing: str | Path, uploaded: str | Path) -> MergeResult | None:
    """Merge an uploaded cookies file into the person's file. None = the upload holds no cookies at all (nothing
    is touched)."""
    new = _read(Path(uploaded))
    if not new:
        return None
    old = _read(Path(existing))
    new_sites = list(dict.fromkeys(entry.site for entry in new))
    kept_entries = [entry for entry in old if entry.site not in new_sites]
    old_sites = {entry.site for entry in old}
    _write(Path(existing), kept_entries + new)
    no_login = [site for site in new_sites if site in _LOGIN_COOKIE
                and not any(entry.site == site and entry.name == _LOGIN_COOKIE[site] for entry in new)]
    return MergeResult(added=[s for s in new_sites if s not in old_sites],
                       replaced=[s for s in new_sites if s in old_sites],
                       kept=sorted({entry.site for entry in kept_entries}), no_login=no_login)


@dataclass
class SiteInfo:
    site: str
    count: int
    expired: bool             # it has expiring cookies and every one is already past its date


def list_sites(path: str | Path) -> list[SiteInfo]:
    entries = _read(Path(path))
    now = time.time()
    infos = []
    for site in dict.fromkeys(entry.site for entry in entries):
        mine = [entry for entry in entries if entry.site == site]
        dated = [entry.expiry for entry in mine if entry.expiry > 0]
        has_session_cookie = any(entry.expiry <= 0 for entry in mine)        # those never expire on their own
        infos.append(SiteInfo(site, len(mine), bool(dated) and not has_session_cookie
                              and all(expiry < now for expiry in dated)))
    return sorted(infos, key=lambda info: site_label(info.site).lower())


def remove_site(path: str | Path, site: str) -> bool:
    """Delete one site's cookies (the whole file when it was the last). True = something was removed."""
    path = Path(path)
    entries = _read(path)
    remaining = [entry for entry in entries if entry.site != site]
    if len(remaining) == len(entries):
        return False
    if remaining:
        _write(path, remaining)
    else:
        path.unlink(missing_ok=True)
    return True
