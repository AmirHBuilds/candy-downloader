"""Timestamp parsing and section validation (pure logic, no yt-dlp / telegram)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from downloader.sections import (  # noqa: E402
    MAX_SECTIONS, SectionDraft, SectionError, check_can_add, filename_tag, format_section, format_timestamp,
    make_section, parse_timestamp, validate_sections,
)


class ParseTimestamp(unittest.TestCase):
    def test_accepted_formats(self):
        for text, seconds in [("1:50:00", 6600), ("50:00", 3000), ("110:00", 6600), ("90", 90),
                              ("0", 0), ("1:05.5", 65.5), ("  1:50:00 ", 6600)]:
            with self.subTest(text=text):
                self.assertEqual(parse_timestamp(text), seconds)

    def test_persian_and_fullwidth_input(self):
        self.assertEqual(parse_timestamp("۱:۵۰:۰۰"), 6600)
        self.assertEqual(parse_timestamp("١:٠٥"), 65)
        self.assertEqual(parse_timestamp("1：05"), 65)

    def test_rejected(self):
        for text in ["", "   ", "abc", "1:2:3:4", "-5", "1:", ":30", "1.5:00", "1:75", "1:75:00", "1:60"]:
            with self.subTest(text=text), self.assertRaises(SectionError):
                parse_timestamp(text)

    def test_error_messages_are_helpful(self):
        with self.assertRaisesRegex(SectionError, "1:50:00"):
            parse_timestamp("nonsense")
        with self.assertRaisesRegex(SectionError, "Seconds must be below 60"):
            parse_timestamp("1:75")


class Formatting(unittest.TestCase):
    def test_format_timestamp(self):
        for seconds, text in [(6600, "1:50:00"), (307, "5:07"), (0, "0:00"), (90.5, "1:30.5"),
                              (59.96, "1:00"), (3599.95, "1:00:00")]:
            with self.subTest(seconds=seconds):
                self.assertEqual(format_timestamp(seconds), text)

    def test_round_trip(self):
        for seconds in (0, 59, 60, 3599, 3600, 6600, 7384):
            self.assertEqual(parse_timestamp(format_timestamp(seconds)), seconds)

    def test_section_text_and_filename_tag(self):
        self.assertEqual(format_section(6600, 6720), "1:50:00 – 1:52:00")
        self.assertEqual(filename_tag(6600, 6720), "01-50-00–01-52-00")
        self.assertNotIn(":", filename_tag(6600, 6720))       # Windows / Telegram safe


class MakeSection(unittest.TestCase):
    def test_empty_start_means_from_the_beginning(self):
        self.assertEqual(make_section(None, 120, 6194), (0.0, 120.0))

    def test_empty_end_means_to_the_end(self):
        self.assertEqual(make_section(6000, None, 6194), (6000.0, 6194.0))

    def test_both_empty_is_rejected(self):
        with self.assertRaisesRegex(SectionError, "can't both be empty"):
            make_section(None, None, 6194)

    def test_end_must_follow_start(self):
        for start, end in [(200, 100), (100, 100)]:
            with self.subTest(start=start, end=end), self.assertRaisesRegex(SectionError, "after the start"):
                make_section(start, end, 1000)

    def test_bounds_against_video_length(self):
        with self.assertRaisesRegex(SectionError, "Start .* past the end"):
            make_section(2000, None, 1000)
        with self.assertRaisesRegex(SectionError, "End .* past the end"):
            make_section(0, 1001, 1000)
        self.assertEqual(make_section(0, 1000, 1000), (0.0, 1000.0))    # exactly the end is fine

    def test_minimum_length(self):
        with self.assertRaisesRegex(SectionError, "at least 1 second"):
            make_section(5, 5.5, 100)

    def test_unknown_duration_skips_upper_bounds_only(self):
        self.assertEqual(make_section(10, 5000, None), (10.0, 5000.0))
        with self.assertRaises(SectionError):
            make_section(10, None, None)        # nothing to fill the empty end with


class ValidateSections(unittest.TestCase):
    def test_keeps_order_and_drops_exact_duplicates(self):
        self.assertEqual(validate_sections([(10, 20), (1, 5), (10, 20)], 100), [(10.0, 20.0), (1.0, 5.0)])

    def test_overlaps_are_allowed(self):
        self.assertEqual(len(validate_sections([(0, 10), (5, 15)], 100)), 2)

    def test_empty_list_and_junk_are_rejected(self):
        for bad in ([], None, [(1,)], ["ab"], [None]):
            with self.subTest(bad=bad), self.assertRaises(SectionError):
                validate_sections(bad, 100)

    def test_limit(self):
        many = [(i * 10, i * 10 + 5) for i in range(MAX_SECTIONS + 1)]
        with self.assertRaisesRegex(SectionError, str(MAX_SECTIONS)):
            validate_sections(many, 10_000)
        check_can_add(MAX_SECTIONS - 1)
        with self.assertRaises(SectionError):
            check_can_add(MAX_SECTIONS)


class Draft(unittest.TestCase):
    D = 6194

    def test_starts_with_one_empty_row_and_no_sections(self):
        draft = SectionDraft()
        self.assertEqual(len(draft.rows), 1)
        self.assertTrue(draft.rows[0].is_empty())
        self.assertEqual(draft.active(self.D), [])

    def test_a_single_value_makes_a_section(self):
        draft = SectionDraft()
        draft.set_value(0, "end", 120, self.D)
        self.assertEqual(draft.active(self.D), [(0.0, 120.0)])
        draft.clear_value(0, "end")
        draft.set_value(0, "start", 6000, self.D)
        self.assertEqual(draft.active(self.D), [(6000.0, 6194.0)])

    def test_values_are_validated_together_with_the_other_side_and_nothing_changes_on_error(self):
        draft = SectionDraft()
        draft.set_value(0, "start", 600, self.D)
        for which, value in [("end", 300), ("end", 9999), ("start", 7000)]:
            with self.subTest(which=which, value=value), self.assertRaises(SectionError):
                draft.set_value(0, which, value, self.D)
        self.assertEqual((draft.rows[0].start, draft.rows[0].end), (600, None))

    def test_moving_start_past_an_existing_end_is_refused(self):
        draft = SectionDraft()
        draft.set_value(0, "start", 100, self.D)
        draft.set_value(0, "end", 200, self.D)
        with self.assertRaisesRegex(SectionError, "after the start"):
            draft.set_value(0, "start", 250, self.D)

    def test_add_row_needs_the_last_row_filled(self):
        draft = SectionDraft()
        with self.assertRaisesRegex(SectionError, "Fill in the current section"):
            draft.add_row()
        draft.set_value(0, "end", 60, self.D)
        draft.add_row()
        self.assertEqual(len(draft.rows), 2)
        with self.assertRaises(SectionError):
            draft.add_row()                       # the new one is empty now

    def test_the_row_limit(self):
        draft = SectionDraft()
        for i in range(MAX_SECTIONS):
            draft.set_value(len(draft.rows) - 1, "end", (i + 1) * 60, self.D)
            if i < MAX_SECTIONS - 1:
                draft.add_row()
        with self.assertRaisesRegex(SectionError, str(MAX_SECTIONS)):
            draft.add_row()

    def test_remove_keeps_order_and_always_leaves_a_row(self):
        draft = SectionDraft()
        for end in (60, 120, 180):
            draft.set_value(len(draft.rows) - 1, "end", end, self.D)
            draft.add_row() if end != 180 else None
        draft.remove_row(1)
        self.assertEqual(draft.active(self.D), [(0.0, 60.0), (0.0, 180.0)])
        draft.remove_row(0)
        draft.remove_row(0)
        self.assertEqual(len(draft.rows), 1)
        self.assertTrue(draft.rows[0].is_empty())

    def test_stale_indexes_raise_a_readable_error(self):
        draft = SectionDraft()
        for call in (lambda: draft.remove_row(5), lambda: draft.set_value(5, "end", 10, self.D),
                     lambda: draft.clear_value(5, "end")):
            with self.assertRaisesRegex(SectionError, "no longer exists"):
                call()

    def test_empty_rows_in_the_middle_are_ignored_and_duplicates_dropped(self):
        draft = SectionDraft()
        draft.set_value(0, "end", 60, self.D)
        draft.add_row()                           # stays empty
        self.assertEqual(draft.active(self.D), [(0.0, 60.0)])
        draft.set_value(1, "end", 60, self.D)     # identical to row 0
        self.assertEqual(draft.active(self.D), [(0.0, 60.0)])

    def test_merge_flag_defaults_to_separate(self):
        self.assertFalse(SectionDraft().merge)


if __name__ == "__main__":
    unittest.main()
