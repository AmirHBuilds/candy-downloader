"""Which symbol each kind of status line gets."""
import unittest

from tests import _env  # noqa: F401

from ui.steplog import step_line, symbol_for  # noqa: E402

CASES = [
    ("Starting…", "★"), ("Fetching…", "★"), ("Queued", "★"),
    ("Trying yt-dlp...", "⌲"),
    ("Waiting for a free slot.", "ⴵ"), ("12s elapsed", "ⴵ"),
    ("Clip 1 of 2 · 1:20:12 – 1:21:42", "✄"), ("Clip · 0:02 – 0:04", "✄"),
    ("Clip 1 of 2 · 1:20:12 – 1:21:42 · 0:44", "ⴵ"), ("Clip · 0:02 – 0:04 · 1:05", "ⴵ"),
    # video / audio: stream bars (finished or not) and their processing steps
    ("Video - 42% • 🍬🍬🍬🍬◾️◾️◾️◾️◾️◾️ • 2.1MB/s", "✦"), ("Video - 100%", "✦"), ("Converting video", "✦"),
    ("Audio - 7%", "𝄞"), ("Audio - 100%", "𝄞"), ("Extracting audio", "𝄞"),
    ("Merging video & audio", "✶"), ("Merging clips", "✶"), ("Embedding thumbnail", "✶"),
    ("Adding metadata", "⛃"), ("Moving file", "⇄"),
    ("Uploading to Telegram...", "🗫"), ("Sending 12.3 MB...", "🗫"),
    ("42% • 🍬🍬◾️", "⫶☰"), ("100%", "✓"),                       # no label (files, galleries)
    ("yt-dlp failed: boom", "✕"), ("gallery-dl: produced no files", "✕"),
    ("None of our downloaders support this link.", "✕"), ("Cancelled", "✕"),
    ("Cancelled before it started", "✕"), ("Couldn't merge - sending the clips separately", "✕"),
    ("Something we never anticipated", "→"),
]


class StepSymbols(unittest.TestCase):
    def test_symbols(self):
        for text, symbol in CASES:
            with self.subTest(text=text):
                self.assertEqual(symbol_for(text), symbol)

    def test_line_format(self):
        self.assertEqual(step_line("Trying yt-dlp..."), "[⌲] Trying yt-dlp...")
        self.assertEqual(step_line("Video - 100%"), "[✦] Video - 100%")
        self.assertEqual(step_line("Audio - 5%"), "[𝄞] Audio - 5%")

    def test_none_of_the_glyphs_is_an_emoji_presentation_character(self):
        # Telegram draws anything with emoji presentation as a coloured picture.
        for _, symbol in CASES:
            for ch in symbol:
                self.assertNotIn(ch, "✂✅❌⭐⚙🎬🎵")


if __name__ == "__main__":
    unittest.main()
