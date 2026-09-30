"""Import this FIRST in a test module: puts the stub telegram/yt_dlp/httpx packages
ahead of any real ones, and gives config.py the env vars it insists on."""
import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_USER_ID", "1")

_HERE = Path(__file__).resolve().parent
for path in (str(_HERE / "stubs"), str(_HERE.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)
