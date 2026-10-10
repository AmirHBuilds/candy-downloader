"""
Passwords, tokens and rate limits for the web app. Standard library only, no home-made cryptography:
scrypt for passwords, `secrets` for tokens, `hmac.compare_digest` for comparisons.
"""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

# scrypt: 16 MiB per hash, ~50 ms. Strong enough for a personal service, light enough for a small VPS.
_N, _R, _P = 2 ** 14, 8, 1
_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200            # scrypt takes long inputs happily, but there is no reason to accept a megabyte

_PW_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no look-alike characters


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Used when the username does not exist, so "no such user" costs the same time as "wrong password".
_DUMMY_HASH = hash_password("not-a-real-password")


def burn_time() -> None:
    verify_password("x", _DUMMY_HASH)


def check_password_policy(password: str, username: str = "") -> str:
    """'' when fine, otherwise what is wrong (plain text for the person)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    if username and password.lower() == username.lower():
        return "The password can't be the same as the username."
    if len(set(password)) < 5:
        return "That password is too repetitive."
    return ""


def generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Session tokens are stored hashed: a copy of the database does not hand out logged-in sessions."""
    return hashlib.sha256(token.encode()).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    """At most `limit` hits per `window` seconds per key (sliding window, in memory)."""

    def __init__(self, limit: int, window: float, clock=time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str) -> deque:
        hits = self._hits[key]
        cutoff = self._clock() - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
            return deque()
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def hit(self, key: str) -> bool:
        """Count one attempt. False = over the limit (the attempt is not counted)."""
        hits = self._trim(key)
        if len(hits) >= self.limit:
            return False
        self._hits[key].append(self._clock())
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        hits = self._trim(key)
        if len(hits) < self.limit:
            return 0
        return max(1, int(hits[0] + self.window - self._clock()) + 1)
