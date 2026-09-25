from html import escape as _esc

from config import OWNER_NAME, OWNER_EMOJI

WELCOME = f"{OWNER_EMOJI} <b>{OWNER_NAME}'s Downloader</b>\n\n→ Send me a link to get started."

PICK_OPTION = "What do you want?"
QUEUED = f"{OWNER_EMOJI} Queued"
UPLOADING = "Sending…"
CANCELLED = "✕ Cancelled"
NOTHING_TO_CANCEL = "Nothing running for you right now."
PROBING = "Checking link…"

PRIVATE_BOT = f"• This is {OWNER_NAME}'s private bot. Ask her to add you."


def with_link(text: str, url: str) -> str:
    """Keeps the link visible (and easily copyable, via Telegram's
    tap-to-copy on <code> blocks) throughout the message's lifecycle,
    since we delete the person's original message."""
    return f"{text}\n\n<code>{_esc(url)}</code>"


def join_required(channel: str) -> str:
    return f"→ Join {channel} first to use this bot."


def preview_failed_note(error: str) -> str:
    """Shown when we couldn't fetch a title/thumbnail/quality preview.
    Distinguishes a login-wall (common, worth explaining) from a plain
    unknown hiccup, and always offers to try anyway rather than dead-ending."""
    low = (error or "").lower()
    if any(marker in low for marker in ("sign in", "cookies", "log in", "login", "logged-in")):
        return (
            "Couldn't load a preview — this needs a login to even check. "
            "You can still try downloading, but it may fail too. "
            "/cookies fixes this properly."
        )
    return "Couldn't load a preview for this link, but you can still try downloading it."


def unsupported_link() -> str:
    return "✕ Couldn't grab that — unsupported site, or it needs a login (see /cookies)."


def generic_error(detail: str) -> str:
    # detail is tool/error output - inherently untrusted (a page's title,
    # an error string echoed from a remote server, etc.) - escape before
    # it goes into <code>, or a stray '<' makes Telegram reject the whole
    # message ("can't parse entities") and the person just sees nothing.
    short = _esc((detail or "").strip()[:200]) or "Unknown error"
    if any(marker in short.lower() for marker in ("sign in", "cookies", "log in", "login", "logged-in")):
        return (
            f"✕ Didn't work:\n<code>{short}</code>\n\n"
            "This content needs a login. Send /cookies to fix it."
        )
    return f"✕ Didn't work:\n<code>{short}</code>"


def all_done_caption(title: str) -> str:
    return f"{OWNER_EMOJI} {_esc(title)}"
