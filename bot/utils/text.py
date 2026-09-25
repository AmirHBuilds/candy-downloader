"""
Small pure text helpers (no Telegram / no config imports, so they're easy
to unit-test - see bot/tests/).

Two kinds of text flow through this bot into Telegram's HTML parse mode:
text WE write (safe as-is) and text that comes from OUTSIDE - video
titles, yt-dlp/gallery-dl error strings, URLs. The outside kind must be
HTML-escaped, otherwise a title like "I <3 this" makes Telegram reject the
whole message ("can't parse entities"). `clean_step` = sanitize + escape.
"""
import re
from html import escape as _escape
from urllib.parse import urlparse


def esc(text: str) -> str:
    """HTML-escape untrusted text for Telegram's HTML parse mode."""
    return _escape(text or "", quote=False)


_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_ERROR_PREFIX_RE = re.compile(r"(?i)\berror:\s*")
# ID-like tokens (must contain a digit, 6+ chars) followed by a colon, e.g.
# "dQw4w9WgXcQ:". The digit lookahead matters: without it this also ate
# legitimate words such as "gallery-dl:" or "failed:".
_ID_COLON_RE = re.compile(r"\b(?=[\w-]*\d)[\w-]{6,}:\s*")
_BOILERPLATE_CUTS = (
    "; please report", "please report this issue",
    "Confirm you are on the latest version",
)


def sanitize_step(text: str, url: str) -> str:
    """Turns a raw tool/error string into a short, readable PLAIN-text line.
    (Not HTML-safe yet - use clean_step for anything that goes to Telegram.)"""
    cleaned = (text or "")
    if url:
        cleaned = cleaned.replace(url, "this link")
    for cut in _BOILERPLATE_CUTS:
        idx = cleaned.find(cut)
        if idx != -1:
            cleaned = cleaned[:idx]
    cleaned = _ERROR_PREFIX_RE.sub("", cleaned)
    cleaned = _BRACKET_RE.sub("", cleaned)
    cleaned = _ID_COLON_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .;")
    if len(cleaned) > 140:
        cleaned = cleaned[:137].rstrip() + "..."
    return cleaned or "failed"


def clean_step(text: str, url: str) -> str:
    """sanitize_step + HTML-escape: safe to drop straight into a message."""
    return esc(sanitize_step(text, url))


def friendly_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return (host[4:] if host.startswith("www.") else host) or "this link"
    except Exception:  # noqa: BLE001
        return "this link"
