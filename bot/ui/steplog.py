"""
Symbols for the live status log, one per kind of step:

    [★] Starting…
    [⌲] Trying yt-dlp
    [✄] Clip 1 of 2 · 1:20:12 – 1:21:42
    [ⴵ] Clip 1 of 2 · 1:20:12 – 1:21:42 · 0:44
    [⫶☰] Video - 42% • ...
    [✦] Video - 100%        (✦ video, 𝄞 audio - for their bars and their processing steps)

Steps are plain strings produced all over the code base (dispatcher, handlers,
job manager), so the symbol is chosen from the TEXT here, in one place, rather
than threading a "kind" argument through every progress callback. First
matching rule wins. All glyphs are text symbols, not emoji, so they render
the same everywhere. Matching is on the already-escaped step text, which only
matters for '<', '>' and '&' - none of the patterns use them.
"""
import re

_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^cancel"), "✕"),
    (re.compile(r"\bfailed\b|produced no files|^none of our|^couldn.?t|timed out|^error"), "✕"),
    (re.compile(r"^waiting"), "ⴵ"),
    (re.compile(r"·\s*\d+:\d{2}$|\d+s elapsed$"), "ⴵ"),               # elapsed-time ticker
    (re.compile(r"^trying\b"), "⌲"),
    (re.compile(r"^(queued|starting|fetching)"), "★"),
    (re.compile(r"^(uploading|sending)"), "🗫"),                      # on its way to Telegram
    (re.compile(r"^clip\b"), "✄"),
    (re.compile(r"^adding metadata|^metadata"), "⛃"),
    (re.compile(r"^moving|^movefiles"), "⇄"),
    (re.compile(r"^(cutting|trimming)"), "✄"),                        # the toolbox's trim
    (re.compile(r"^(burning|compressing|making the gif)"), "✶"),       # the toolbox's heavy re-encodes
    (re.compile(r"^merging"), "✶"),                                   # before the audio/video rules: "Merging video & audio" is both
    (re.compile(r"^audio\b|extracting audio|converting audio"), "𝄞"),   # Audio stream bars, audio extraction
    (re.compile(r"^video\b|converting video"), "✦"),                 # Video stream bars
    (re.compile(r"^(converting|extracting|embedding|adding|fixing|finishing|writing|processing|"
                r"modifying|applying|removing|splitting|fixup)"), "✶"),
    (re.compile(r"\b100%$"), "✓"),                                    # an unlabelled bar that just finished
    (re.compile(r"\d+%"), "⫶☰"),                                      # unlabelled progress bars
]
_DEFAULT = "→"


def symbol_for(text: str) -> str:
    lowered = text.strip().lower()
    for pattern, symbol in _RULES:
        if pattern.search(lowered):
            return symbol
    return _DEFAULT


def step_line(text: str) -> str:
    """'[⌲] Trying yt-dlp' - the symbol is always wrapped in brackets."""
    return f"[{symbol_for(text)}] {text}"
