"""Secrets the web app has to be able to read back (an uploader's API key): encrypted at rest with Fernet.
The key comes from WEB_SECRET_KEY, or is generated once into data/web_secret (mode 0600)."""
import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import DATA_DIR

_fernet: Fernet | None = None


def _secret() -> bytes:
    configured = os.environ.get("WEB_SECRET_KEY", "").strip()
    if configured:
        return configured.encode()
    path = Path(DATA_DIR) / "web_secret"
    if path.exists():
        return path.read_bytes().strip()
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    value = base64.urlsafe_b64encode(os.urandom(32))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(value)
    return value


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_secret()).digest()))
    return _fernet


def reset() -> None:
    global _fernet
    _fernet = None


def encrypt(text: str) -> str:
    return _get().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _get().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""            # key changed or data damaged: behave as "not set"
