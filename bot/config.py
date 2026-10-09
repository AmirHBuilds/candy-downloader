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


# --- Site routing (see downloader/proxy.py) ---------------------------------------------------
# Comma-separated proxy URLs used ONLY for the sites in PROXY_DOMAINS, e.g. socks5h://warp:1080 (the bundled WARP container).
# Empty = never use a proxy. socks5h makes the proxy do the DNS lookup too.
YT_PROXIES = [p.strip() for p in os.getenv("YT_PROXIES", "").split(",") if p.strip()]
# auto   = go direct; if a site blocks this server's address, switch to the proxy for that site for a while
# always = always use the proxy when it is reachable
# off    = never
YT_PROXY_MODE = os.getenv("YT_PROXY_MODE", "auto").strip().lower()
# Which sites may be routed through the proxy (a site is also matched by its subdomains). "*" = every site.
# Each site is tracked on its own: YouTube blocking this server does not send Instagram through the proxy.
_DEFAULT_PROXY_DOMAINS = ("youtube.com,youtu.be,x.com,twitter.com,instagram.com,pinterest.com,pin.it,"
                          "reddit.com,tiktok.com")
PROXY_DOMAINS = [d.strip().lower() for d in os.getenv("PROXY_DOMAINS", _DEFAULT_PROXY_DOMAINS).split(",") if d.strip()]
# Politeness for the host machine: child processes (ffmpeg!) inherit this, so a conversion can't starve
# your PC. 0 = off; 10 is "low priority".
PROCESS_NICE = int(os.getenv("PROCESS_NICE", "10"))

# Burned-in subtitles re-encode the whole video (slow, heavy on a small machine): longer videos are
# sent with the subtitles as a separate .srt instead. 0 = no limit.
BURN_MAX_SECONDS = int(os.getenv("BURN_MAX_SECONDS", str(60 * 60))) or 10 ** 9

# --- Housekeeping (see utils/housekeeping.py) ----------------------------------------------------
HOUSEKEEPING_INTERVAL_SECONDS = int(os.getenv("HOUSEKEEPING_INTERVAL_SECONDS", "300"))
# The local Bot API server keeps a copy of every file anyone sends the bot (videos, cookies.txt, ...). The bot
# deletes its copy as soon as it has its own, and sweeps anything older than this as a safety net.
BOT_API_DATA_DIR = os.getenv("BOT_API_DATA_DIR", "/var/lib/telegram-bot-api").rstrip("/")
BOT_API_FILE_MAX_AGE_MINUTES = int(os.getenv("BOT_API_FILE_MAX_AGE_MINUTES", "60"))
BOT_API_CLEANUP = os.getenv("BOT_API_CLEANUP", "on").strip().lower() != "off"

# --- Changing the WARP address from the admin panel (see downloader/warp_control.py) -----------------------------
# The control service (warp_control/ in the repo, the `warp-control` compose service) re-registers and restarts the
# WARP container. Empty URL = the button explains how to do it by hand instead.
WARP_CONTROL_URL = os.getenv("WARP_CONTROL_URL", "").strip().rstrip("/")
WARP_CONTROL_TOKEN = os.getenv("WARP_CONTROL_TOKEN", "").strip()
WARP_ROTATE_COOLDOWN_SECONDS = int(os.getenv("WARP_ROTATE_COOLDOWN_SECONDS", "120"))
