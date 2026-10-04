"""
Keeps the bot token out of the logs.

python-telegram-bot / httpx log every request URL at INFO, and the Bot API URL
contains the token (".../bot<id>:<secret>/getUpdates", roughly every 10
seconds). Pasting logs for help therefore leaked it. Two layers:

  1. httpx / httpcore are turned down to WARNING - that removes the per-request
     lines (and the noise that buried the useful ones).
  2. Everything that is still logged goes through RedactingFormatter, which
     scrubs the FINAL text - including exception tracebacks, where a failing
     request echoes its URL (a filter on the record can't see those).
"""
import logging
import re

# Telegram bot tokens look like 1234567890:AAH-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.
_TOKEN = re.compile(r"(?<![A-Za-z0-9])(bot)?\d{6,}:[A-Za-z0-9_-]{30,}")


def redact(text: str, secret: str | None = None) -> str:
    """Hide anything shaped like a bot token, plus the configured token itself
    (in case its format ever differs). The "bot" URL prefix is kept so logs
    still show which endpoint was called."""
    if secret:
        text = text.replace(secret, "<redacted>")
    return _TOKEN.sub(lambda m: "bot<redacted>" if m.group(1) else "<redacted>", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str | None = None, secret: str | None = None, **kwargs):
        super().__init__(fmt, **kwargs)
        self._secret = secret

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secret)


def install_safe_logging(secret: str | None = None) -> None:
    """Call once, right after logging.basicConfig()."""
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        old = handler.formatter
        fmt = getattr(old, "_fmt", None) if old else None
        datefmt = getattr(old, "datefmt", None) if old else None
        handler.setFormatter(RedactingFormatter(fmt, secret, datefmt=datefmt))
