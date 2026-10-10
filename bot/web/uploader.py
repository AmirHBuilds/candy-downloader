"""
The "Get a link" uploader (e.g. a candyflix instance): sends a file to a configured HTTP endpoint and returns the link
it answers with. Generic on purpose, so it matches whatever the target API looks like:

  url           where to POST the file (multipart/form-data)
  file_field    the form field name of the file ("file")
  auth_header   header that carries the key ("Authorization", "X-API-Key", ...); blank = no key
  auth_prefix   text put before the key in that header ("Bearer ")
  extra_fields  more form fields to send along (JSON object of strings)
  response_path where the link is in the JSON answer: "data.url", "files.0.link"; blank = find the first http(s) link
  max_mb        refuse bigger files here instead of failing late
  allow_user_keys  let each person use their own key (the URL stays the admin's: never user-chosen, so nobody can
                point the server at internal addresses)

The key is stored encrypted and is never returned by any API: only "a key is set".
"""
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from config import DB_PATH, DATA_DIR
from web import secrets_store

GLOBAL = "global"
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
TIMEOUT_SECONDS = 900


class UploaderError(Exception):
    """User-facing, plain text, never contains the key."""


DEFAULT = {"enabled": False, "url": "", "file_field": "file", "auth_header": "Authorization",
           "auth_prefix": "Bearer ", "extra_fields": {}, "response_path": "", "max_mb": 500,
           "allow_user_keys": False}


def _connect() -> sqlite3.Connection:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS uploader_config (scope TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.commit()
    return conn


def _load(scope: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute("SELECT data FROM uploader_config WHERE scope = ?", (scope,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        return json.loads(secrets_store.decrypt(row[0]) or "{}")
    except ValueError:
        return {}


def _save(scope: str, data: dict) -> None:
    conn = _connect()
    try:
        conn.execute("INSERT INTO uploader_config (scope, data) VALUES (?, ?) "
                     "ON CONFLICT(scope) DO UPDATE SET data = excluded.data",
                     (scope, secrets_store.encrypt(json.dumps(data))))
        conn.commit()
    finally:
        conn.close()


def get_global() -> dict:
    return {**DEFAULT, **_load(GLOBAL)}


def public_global() -> dict:
    """For the admin screen: everything except the key itself."""
    config = get_global()
    key = config.pop("key", "")
    config["has_key"] = bool(key)
    return config


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise UploaderError("The address must look like https://host/path (no user:password inside).")
    return url


def set_global(update: dict) -> dict:
    """Merge the admin's changes. A missing or empty 'key' keeps the stored one; 'clear_key': true removes it."""
    current = get_global()
    for name in ("file_field", "auth_header", "auth_prefix", "response_path"):
        if name in update:
            value = str(update[name])[:200]
            if name == "file_field" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", value):
                raise UploaderError("The file field name may only contain letters, digits and _ . -")
            if name == "auth_header" and value and not re.fullmatch(r"[A-Za-z0-9-]{1,60}", value):
                raise UploaderError("That isn't a valid header name.")
            current[name] = value
    if "url" in update:
        current["url"] = validate_url(update["url"]) if str(update["url"]).strip() else ""
    for name in ("enabled", "allow_user_keys"):
        if name in update:
            current[name] = bool(update[name])
    if "max_mb" in update:
        try:
            current["max_mb"] = max(1, min(int(update["max_mb"]), 2048))
        except (TypeError, ValueError):
            raise UploaderError("Maximum size must be a number of MB.")
    if "extra_fields" in update:
        fields = update["extra_fields"]
        if not isinstance(fields, dict) or len(fields) > 10 or not all(
                isinstance(k, str) and isinstance(v, str) and len(k) <= 60 and len(v) <= 300 for k, v in fields.items()):
            raise UploaderError("Extra fields must be a small JSON object of text values.")
        current["extra_fields"] = fields
    if update.get("clear_key"):
        current.pop("key", None)
    elif str(update.get("key") or "").strip():
        current["key"] = str(update["key"]).strip()[:500]
    if current["enabled"] and not current["url"]:
        raise UploaderError("Set the upload address before turning the uploader on.")
    _save(GLOBAL, current)
    return public_global()


def _user_scope(user_id: int) -> str:
    return f"user:{user_id}"


def user_status(user_id: int) -> dict:
    config = get_global()
    mine = _load(_user_scope(user_id))
    return {"available": bool(config["enabled"] and config["url"]), "allow_user_keys": bool(config["allow_user_keys"]),
            "has_own_key": bool(mine.get("key")), "has_shared_key": bool(config.get("key")),
            "max_mb": config["max_mb"]}


def set_user_key(user_id: int, key: str) -> None:
    if not get_global()["allow_user_keys"]:
        raise UploaderError("Personal keys aren't enabled here.")
    key = (key or "").strip()
    if not key:
        conn = _connect()
        try:
            conn.execute("DELETE FROM uploader_config WHERE scope = ?", (_user_scope(user_id),))
            conn.commit()
        finally:
            conn.close()
        return
    _save(_user_scope(user_id), {"key": key[:500]})


def extract_link(body: str, path: str) -> str:
    """The link in the server's answer."""
    link = ""
    if path:
        try:
            node = json.loads(body)
            for part in path.split("."):
                node = node[int(part)] if isinstance(node, list) else node[part]
            link = node if isinstance(node, str) else ""
        except (ValueError, KeyError, IndexError, TypeError):
            link = ""
    if not link:
        match = _URL_RE.search(body or "")
        link = match.group(0) if match else ""
    if not link or urlparse(link).scheme not in ("http", "https"):
        raise UploaderError("The uploader answered, but I couldn't find a link in its reply.")
    return link


def _config_for(user_id: int) -> tuple[dict, str]:
    config = get_global()
    if not (config["enabled"] and config["url"]):
        raise UploaderError("The uploader isn't set up yet. An admin can do that in the web app (Admin > Uploader).")
    key = config.get("key", "")
    if config["allow_user_keys"]:
        key = _load(_user_scope(user_id)).get("key") or key
    return config, key


async def _post(url: str, headers: dict, fields: dict, field: str, path: Path, name: str) -> tuple[int, str]:
    import httpx
    with open(path, "rb") as fh:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, data=fields, files={field: (name, fh)})
    return response.status_code, response.text[:20000]


async def upload(path: Path, name: str, user_id: int) -> str:
    """Send the file; return the link."""
    config, key = _config_for(user_id)
    size_mb = Path(path).stat().st_size / 1_000_000
    if size_mb > config["max_mb"]:
        raise UploaderError(f"That file is {size_mb:.0f} MB; the uploader accepts up to {config['max_mb']} MB.")
    headers = {}
    if key and config["auth_header"]:
        headers[config["auth_header"]] = f"{config['auth_prefix']}{key}"
    try:
        status, body = await _post(config["url"], headers, dict(config["extra_fields"]), config["file_field"],
                                   Path(path), name)
    except UploaderError:
        raise
    except Exception as exc:  # noqa: BLE001 - network trouble: say so, without echoing anything sensitive
        raise UploaderError(f"Couldn't reach the uploader ({type(exc).__name__}).") from None
    if status in (401, 403):
        raise UploaderError("The uploader refused the key (it may be wrong or expired).")
    if status >= 400:
        raise UploaderError(f"The uploader answered with an error ({status}).")
    return extract_link(body, config["response_path"])
