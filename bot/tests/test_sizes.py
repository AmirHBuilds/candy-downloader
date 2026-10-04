"""Estimated sizes on the quality buttons."""
import unittest

from tests import _env  # noqa: F401

from downloader.sizes import estimate_sizes, format_size, size_labels  # noqa: E402

DURATION = 6194
MB = 1_000_000


def video(height, size, codec="avc1.64001f", **extra):
    return {"height": height, "vcodec": codec, "acodec": "none", "filesize": size, **extra}


def audio(size, codec="mp4a.40.2", abr=130):
    return {"vcodec": "none", "acodec": codec, "filesize": size, "abr": abr}


class Estimates(unittest.TestCase):
    def test_height_is_video_plus_audio(self):
        sizes = estimate_sizes([video(720, 400 * MB), audio(100 * MB)], DURATION, [720])
        self.assertEqual(sizes["720"], 500 * MB)

    def test_follows_the_h264_preference_not_just_the_tallest(self):
        """The bot's selector wants H.264; a taller VP9-only rendition must not be quoted."""
        formats = [video(2160, 3000 * MB, "vp09"), video(1080, 800 * MB), audio(100 * MB)]
        sizes = estimate_sizes(formats, DURATION, [2160, 1080])
        self.assertEqual(sizes["best"], 900 * MB)
        self.assertEqual(sizes["2160"], 900 * MB)

    def test_falls_back_to_any_codec_when_there_is_no_h264(self):
        sizes = estimate_sizes([video(720, 300 * MB, "vp09"), audio(100 * MB, "opus")], DURATION, [720])
        self.assertEqual(sizes["720"], 400 * MB)

    def test_prefers_aac_audio_for_the_sum(self):
        formats = [video(720, 400 * MB), audio(100 * MB, "mp4a.40.2", 130), audio(120 * MB, "opus", 160)]
        self.assertEqual(estimate_sizes(formats, DURATION, [720])["720"], 500 * MB)

    def test_uses_approximate_size_then_bitrate_when_no_exact_size(self):
        approx = {"height": 480, "vcodec": "avc1", "acodec": "none", "filesize_approx": 200 * MB}
        by_rate = {"height": 360, "vcodec": "avc1", "acodec": "none", "tbr": 800}
        sizes = estimate_sizes([approx, by_rate, audio(50 * MB)], 1000, [480, 360])
        self.assertEqual(sizes["480"], 250 * MB)
        self.assertEqual(sizes["360"], 800 * 125 * 1000 + 50 * MB)

    def test_combined_files_are_used_when_a_site_has_no_separate_streams(self):
        muxed = {"height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 50 * MB}
        self.assertEqual(estimate_sizes([muxed], DURATION, [360])["360"], 50 * MB)

    def test_unknown_sizes_simply_leave_the_key_out(self):
        sizes = estimate_sizes([{"height": 720, "vcodec": "avc1", "acodec": "none"}, audio(10 * MB)], None, [720])
        self.assertNotIn("720", sizes)
        self.assertNotIn("best", sizes)
        self.assertNotIn("mp3", sizes)                       # no duration -> no bitrate-based guess either

    def test_smallest_is_the_lowest_video_plus_the_smallest_audio(self):
        formats = [video(1080, 800 * MB), video(144, 20 * MB), audio(100 * MB, abr=130), audio(30 * MB, "opus", 50)]
        self.assertEqual(estimate_sizes(formats, DURATION, [1080, 144])["worst"], 50 * MB)

    def test_audio_estimates(self):
        sizes = estimate_sizes([audio(100 * MB), audio(90 * MB, "opus", 140)], DURATION, [])
        self.assertEqual(sizes["mp3"], DURATION * 192 * 125)
        self.assertEqual(sizes["opus"], 90 * MB)             # a native opus stream is copied as-is
        sizes = estimate_sizes([audio(100 * MB)], DURATION, [])
        self.assertEqual(sizes["opus"], DURATION * 128 * 125)

    def test_no_formats_at_all(self):
        self.assertEqual(estimate_sizes([], DURATION, []), {"mp3": DURATION * 192 * 125, "opus": DURATION * 128 * 125})


class Display(unittest.TestCase):
    def test_compact_units(self):
        for value, text in [(500, "1KB"), (850_000, "850KB"), (999_999, "1.0MB"), (5_000_000, "5.0MB"),
                            (45_000_000, "45MB"), (9_960_000, "10MB"), (999_400_000, "999MB"),
                            (1_234_000_000, "1.2GB"), (999_900_000, "1.0GB")]:
            with self.subTest(value=value):
                self.assertEqual(format_size(value), text)

    def test_labels_are_marked_as_estimates_and_scale_with_the_clip_share(self):
        sizes = {"720": 500 * MB, "mp3": 150 * MB}
        self.assertEqual(size_labels(sizes), {"720": "~500MB", "mp3": "~150MB"})
        self.assertEqual(size_labels(sizes, scale=0.02), {"720": "~10MB", "mp3": "~3.0MB"})

    def test_zero_sizes_are_not_shown(self):
        self.assertEqual(size_labels({"720": 0}), {})


if __name__ == "__main__":
    unittest.main()
