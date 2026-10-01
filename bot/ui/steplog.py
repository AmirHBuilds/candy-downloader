"""
Symbols for the live status log, one per kind of step:

    [★] Starting…
    [⌲] Trying yt-dlp
    [✄] Clip 1 of 2 · 1:20:12 – 1:21:42
    [ⴵ] Clip 1 of 2 · 1:20:12 – 1:21:42 · 0:44
    [⫶☰] Video - 42% • ...
    [✓] Video - 100%

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
    (re.compile(r"\b100%$"), "✓"),                                    # a stream/clip that just finished
    (re.compile(r"^waiting"), "ⴵ"),
    (re.compile(r"·\s*\d+:\d{2}$|\d+s elapsed$"), "ⴵ"),               # elapsed-time ticker
    (re.compile(r"^trying\b"), "⌲"),
    (re.compile(r"^(queued|starting|fetching)"), "★"),
    (re.compile(r"^(uploading|sending)"), "➴"),
    (re.compile(r"^clip\b"), "✄"),
    (re.compile(r"^(merging|converting|extracting|embedding|adding|fixing|moving|finishing|writing|"
                r"processing|modifying|applying|removing|splitting|fixup)"), "✶"),
    (re.compile(r"\d+%"), "⫶☰"),                                      # progress bars
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
