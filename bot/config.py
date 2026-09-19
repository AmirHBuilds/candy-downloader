import os
import re

BOT_TOKEN = os.environ["BOT_TOKEN"]
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").rstrip("/")

# Developers/maintainers - separate from the owner. Can use /update,
# /potcheck, and can also open the owner's admin panel for maintenance.
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()
}

# The person this bot was built for. Her user ID is hardcoded here on
# purpose (not stored dynamically) - she's always the owner, always has
# full admin-panel access, and always bypasses access-control/force-join.
# Everything she can change dynamically (who else can use the bot, force
# join, etc.) lives in the database instead - see settings/access_control.py
_owner_id_raw = os.environ.get("OWNER_USER_ID", "").strip()
OWNER_USER_ID = int(_owner_id_raw) if _owner_id_raw else None

OWNER_NAME = os.environ.get("OWNER_NAME", "Candy").strip() or "Candy"
OWNER_EMOJI = os.environ.get("OWNER_EMOJI", "🍬").strip() or "🍬"

# Telegram commands may only contain lowercase latin letters, digits and
# underscores. Built once at import time from OWNER_NAME, e.g.
# "Candy" -> "candy_admin", "Amy Rose" -> "amy_rose_admin".
_sanitized_name = re.sub(r"[^a-zA-Z0-9]+", "_", OWNER_NAME).strip("_").lower() or "owner"
OWNER_ADMIN_COMMAND = f"{_sanitized_name}_admin"

AUTO_UPDATE_HOUR_UTC = int(os.environ.get("AUTO_UPDATE_HOUR_UTC", "4"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))

# PO Token provider - fixes YouTube's "sign in to confirm you're not a
# bot" without needing cookies/login. See downloader/ytdlp_handler.py.
BGUTIL_POT_URL = os.environ.get("BGUTIL_POT_URL", "").rstrip("/")

DATA_DIR = "/app/data"
TMP_DIR = "/app/tmp"
CACHE_DIR = "/app/tmp/recent"   # short-lived cache for "send as file" - see jobqueue/job_manager.py
COOKIES_DIR = "/app/cookies"
DB_PATH = os.path.join(DATA_DIR, "candy.db")

# Telegram's own hard ceiling regardless of local server, for sanity checks
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2GB

