"""
The bridge between the job queue and the web app.

A web account's jobs run in the SAME JobManager as the bot's (one queue, one set of workers, one housekeeping loop), but
there is no Telegram chat to answer to. Their "chat id" is a virtual one far outside Telegram's range:

  * JobManager hands a finished web job's files to `deliveries` instead of uploading them;
  * RoutingBot swallows every message/edit aimed at a virtual chat (notes the job wants to tell the person are kept
    for the web to show) and passes everything else to the real bot untouched.

Only this module knows the numbers, so nothing else can confuse a web chat with a real one.
"""
import time
from pathlib import Path

WEB_CHAT_BASE = -(10 ** 17)          # Telegram chat ids never get near this; SQLite and Python ints handle it fine


def chat_id_for(account_id: int) -> int:
    return WEB_CHAT_BASE - int(account_id)


def is_web_chat(chat_id) -> bool:
    return isinstance(chat_id, int) and chat_id <= WEB_CHAT_BASE


def account_of_chat(chat_id: int) -> int:
    return WEB_CHAT_BASE - chat_id


class Deliveries:
    """What finished web jobs produced: files (kept on disk for a while) and notes. The web layer owns policy
    (expiry, ownership); this only holds what a job handed over."""

    def __init__(self) -> None:
        self.files: dict[str, list[dict]] = {}      # rid -> [{"path": Path, "name": str, "size": int}]
        self.notes: dict[str, list[str]] = {}       # rid -> texts
        self.at: dict[str, float] = {}

    def add_file(self, rid: str, path: Path, name: str) -> None:
        self.files.setdefault(rid, []).append({"path": Path(path), "name": name, "size": Path(path).stat().st_size})
        self.at[rid] = time.time()

    def add_note(self, rid: str, text: str) -> None:
        self.notes.setdefault(rid, []).append(text)

    def drop(self, rid: str) -> list[Path]:
        paths = [e["path"] for e in self.files.pop(rid, [])]
        self.notes.pop(rid, None)
        self.at.pop(rid, None)
        return paths


deliveries = Deliveries()
# chat id -> rid of the job currently talking to it; notes (cookie alert, subtitle notice) have no rid of their own
_last_rid_for_chat: dict[int, str] = {}


def remember_job(chat_id: int, rid: str) -> None:
    _last_rid_for_chat[chat_id] = rid


class RoutingBot:
    """Looks like the Telegram Bot to the JobManager. Calls for a virtual web chat are absorbed; the rest go through."""

    _CHAT_FIRST = {"send_message", "send_document", "send_video", "send_audio", "send_photo", "send_animation",
                   "delete_message", "edit_message_text", "edit_message_caption", "edit_message_reply_markup"}

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name not in self._CHAT_FIRST or not callable(attr):
            return attr

        async def call(*args, **kwargs):
            chat_id = kwargs.get("chat_id", args[0] if args and isinstance(args[0], int) else None)
            if is_web_chat(chat_id):
                if name == "send_message":
                    text = kwargs.get("text", args[1] if len(args) > 1 else "")
                    rid = _last_rid_for_chat.get(chat_id)
                    if rid and isinstance(text, str):
                        deliveries.add_note(rid, text)
                return None
            return await attr(*args, **kwargs)
        return call
