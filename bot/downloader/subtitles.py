"""
Subtitles per link: which tracks a video offers, in what order, and what the
person picked.

Pure functions and small dataclasses - no yt-dlp / telegram imports.

Order: Persian first, then English (the two this bot's people use), then every
other language; within each group the human-made subtitles come before
YouTube's auto-generated ones. YouTube offers auto-generated captions in well
over a hundred languages (machine translations), so the list is paged.
"""
from dataclasses import dataclass, field

PRIORITY = ("fa", "en")
MAX_LANGS = 4
TRACKS_PER_PAGE = 8
MODES = ("embed", "file", "both", "burn")
MODE_NAMES = {"embed": "embedded track", "file": "separate .srt file", "both": "embedded + .srt file",
              "burn": "burned into the picture"}

_NAMES = {
    "fa": "Persian", "en": "English", "ar": "Arabic", "tr": "Turkish", "ru": "Russian", "es": "Spanish",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
    "uk": "Ukrainian", "hi": "Hindi", "ur": "Urdu", "bn": "Bengali", "id": "Indonesian", "ms": "Malay",
    "vi": "Vietnamese", "th": "Thai", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "zh-Hans": "Chinese (Simplified)", "zh-Hant": "Chinese (Traditional)", "he": "Hebrew", "iw": "Hebrew",
    "el": "Greek", "sv": "Swedish", "no": "Norwegian", "nb": "Norwegian", "da": "Danish", "fi": "Finnish",
    "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "ku": "Kurdish", "az": "Azerbaijani",
}


class SubtitleError(ValueError):
    """Shown to the person as-is (plain text; escape it before putting it in HTML)."""


@dataclass(frozen=True)
class SubTrack:
    code: str
    name: str
    auto: bool = False


def language_name(code: str, hint: str | None = None) -> str:
    """The site's own name for the track when it gives one, else our table, else the code."""
    if hint and hint.strip():
        return hint.strip()
    if code in _NAMES:
        return _NAMES[code]
    base = code.split("-")[0]
    if base in _NAMES:
        return f"{_NAMES[base]} ({code})"
    return code


def _hint(formats) -> str | None:
    try:
        return formats[0].get("name")
    except (IndexError, AttributeError, TypeError):
        return None


def available_tracks(info: dict) -> list[SubTrack]:
    """Every subtitle track in yt-dlp's info, one per language code (a human-made
    track wins over an auto-generated one for the same code), in display order."""
    tracks: dict[str, SubTrack] = {}
    for code, formats in (info.get("subtitles") or {}).items():
        if code != "live_chat" and formats:                 # live_chat is the stream's chat replay, not subtitles
            tracks[code] = SubTrack(code, language_name(code, _hint(formats)), False)
    for code, formats in (info.get("automatic_captions") or {}).items():
        if code not in tracks and formats:
            tracks[code] = SubTrack(code, language_name(code, _hint(formats)), True)

    def order(track: SubTrack):
        base = track.code.split("-")[0]
        rank = PRIORITY.index(base) if base in PRIORITY else len(PRIORITY)
        return rank, track.auto, track.name.lower()

    return sorted(tracks.values(), key=order)


def page_count(tracks: list) -> int:
    return max(1, -(-len(tracks) // TRACKS_PER_PAGE))


def clamp_page(tracks: list, page: int) -> int:
    return max(0, min(page, page_count(tracks) - 1))


@dataclass
class SubChoice:
    """What the person picked for ONE link: up to MAX_LANGS languages, and how to deliver them."""
    langs: list[str] = field(default_factory=list)
    mode: str = "embed"

    def toggle(self, code: str) -> bool:
        """True = added, False = removed. Raises SubtitleError past the limit."""
        if code in self.langs:
            self.langs.remove(code)
            return False
        if len(self.langs) >= MAX_LANGS:
            raise SubtitleError(f"You can pick up to {MAX_LANGS} languages.")
        self.langs.append(code)
        return True

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise SubtitleError("Unknown option.")
        self.mode = mode

    def settings(self) -> dict:
        return {"sub_langs": list(self.langs), "sub_mode": self.mode}


def summary(choice: SubChoice, tracks: list[SubTrack]) -> str:
    """'Persian, English · embedded track'. Burned in: only the first language is drawn; the rest come as .srt files."""
    names = {t.code: t.name for t in tracks}
    shown = ", ".join(names.get(code, code) for code in choice.langs)
    return f"{shown} · {MODE_NAMES[choice.mode]}"
