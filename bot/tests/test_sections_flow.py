"""
The clip-sections editor end to end: real handlers in main.py, driven with fake
Telegram objects. The editor only edits sections; the quality buttons on the
normal menu then download just those sections.
"""
import time
import unittest

from tests import _env  # noqa: F401  (must come first)

from telegram.error import BadRequest  # noqa: E402  (the stub)

import main  # noqa: E402
from downloader.probe import ProbeResult  # noqa: E402
from downloader.sections import MAX_SECTIONS, SectionDraft, SectionRow  # noqa: E402
from downloader.subtitles import SubTrack  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import quick_menu  # noqa: E402
from ui.section_menu import editor_menu, prompt_menu  # noqa: E402

USER, RID, URL = 42, "rid0000001", "https://www.youtube.com/watch?v=abc"
DURATION = 6194   # 1:43:14


class FakeBot:
    def __init__(self):
        self.edits: list[dict] = []
        self.fail_with: Exception | None = None

    def record(self, kind, text, markup):
        if self.fail_with:
            raise self.fail_with
        self.edits.append({"kind": kind, "text": text, "markup": markup})

    async def edit_message_text(self, text, **kw):
        self.record("text", text, kw.get("reply_markup"))

    async def edit_message_caption(self, caption, **kw):
        self.record("caption", caption, kw.get("reply_markup"))

    @property
    def last(self):
        return self.edits[-1]


class FakeMessage:
    def __init__(self, text="", photo=False):
        self.chat_id, self.message_id, self.text = 7, 99, text
        self.photo = [object()] if photo else []
        self.caption = None

    async def delete(self):
        pass


class FakeQuery:
    """Edits made through the query (the normal menu's set_text) are recorded
    on the same list as edits made through the bot (the editor's)."""

    def __init__(self, data, bot, photo=False):
        self.data, self.bot, self.message = data, bot, FakeMessage(photo=photo)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kw):
        self.bot.record("text", text, kw.get("reply_markup"))

    async def edit_message_caption(self, caption=None, **kw):
        self.bot.record("caption", caption, kw.get("reply_markup"))

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.bot.record("markup", None, reply_markup)


class FakeUpdate:
    def __init__(self, bot, query=None, text=None):
        self.callback_query = query
        self.message = FakeMessage(text) if text is not None else None
        self.effective_user = type("U", (), {"id": USER})()
        self.effective_chat = type("C", (), {"id": 7})()
        self._bot = bot

    def get_bot(self):
        return self._bot


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


class FakeJobManager:
    def __init__(self):
        self.enqueued: list[dict] = []

    async def enqueue(self, rid, user_id, chat_id, url, settings, status_message_id, **kw):
        self.enqueued.append({"rid": rid, "user": user_id, "url": url, "settings": settings, **kw})

    def cancel(self, rid, user_id):
        return False


def buttons(markup):
    return [(b.text, b.callback_data) for b in markup.buttons()]


def datas(markup):
    return [d for _, d in buttons(markup)]


def labels(markup):
    return [t for t, _ in buttons(markup)]


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot, self.jobs = FakeBot(), FakeJobManager()
        for name in ("pending_links", "pending_probes", "pending_sections", "pending_section_input", "last_download",
                     "pending_subs"):
            setattr(main, name, {})
        main.pending_links[RID] = (USER, URL)
        main.pending_probes[RID] = ProbeResult(ok=True, title="My long video", heights=[1080, 720, 480],
                                               has_audio=True, duration=DURATION)
        main.job_manager = self.jobs
        main.get_settings = lambda uid: dict(DEFAULTS)

        async def allow(*a, **k):
            return True
        main.gate_callback = allow
        main.gate = allow
        self.deleted: list = []

        async def record_delete(message):
            self.deleted.append(message)
        main._delete_quietly = record_delete

    # -- helpers
    async def tap(self, data, photo=False):
        await main.link_callback(FakeUpdate(self.bot, FakeQuery(data, self.bot, photo)), FakeContext(self.bot))
        return self.bot.edits[-1] if self.bot.edits else None

    async def type_text(self, text):
        await main.link_handler(FakeUpdate(self.bot, text=text), FakeContext(self.bot))

    async def fill(self, index, field, text):
        await self.tap(f"dl|sec|{field}|{index}|{RID}")
        await self.type_text(text)

    async def add_section(self, start=None, end=None):
        """Add a new row (unless it's the first, empty one) and fill it."""
        draft = main.pending_sections.get(RID)
        if draft is None or not draft.rows[-1].is_empty():
            await self.tap(f"dl|sec|add|{RID}")
        index = len(main.pending_sections[RID].rows) - 1
        if start:
            await self.fill(index, "start", start)
        if end:
            await self.fill(index, "end", end)

    @property
    def draft(self) -> SectionDraft:
        return main.pending_sections[RID]

    # ------------------------------------------------------------ menus
    def test_add_section_lives_in_more_options_and_shows_when_active(self):
        probe = ProbeResult(ok=True, heights=[720], has_audio=True, duration=100)
        none = quick_menu.extended_video_menu(probe, RID)
        self.assertIn("✄ Add section", labels(none))
        two = quick_menu.extended_video_menu(probe, RID, 2)
        self.assertIn("✄ Sections (2) ✓", labels(two))
        self.assertNotIn("✄ Add section", labels(two))
        self.assertIn(f"dl|sec|open|{RID}", datas(two))
        unknown = ProbeResult(ok=True, heights=[720], has_audio=True, duration=None)
        self.assertNotIn(f"dl|sec|open|{RID}", datas(quick_menu.extended_video_menu(unknown, RID, 0)))

    def test_the_main_video_menu_does_not_hold_the_button(self):
        self.assertNotIn(f"dl|sec|open|{RID}", datas(quick_menu.video_menu(main.pending_probes[RID], RID)))

    def test_audio_only_sources_have_it_on_their_one_menu_with_the_mark(self):
        audio_only = ProbeResult(ok=True, heights=[], has_audio=True, duration=100)
        self.assertIn("✄ Add section", labels(quick_menu.quality_menu(audio_only, RID)))
        self.assertIn("✄ Sections (1) ✓", labels(quick_menu.quality_menu(audio_only, RID, 1)))

    def test_every_callback_fits_telegrams_64_byte_limit(self):
        draft = SectionDraft()
        draft.rows = [SectionRow(i * 100.0, i * 100.0 + 50) for i in range(MAX_SECTIONS)]
        draft.merge = True
        for markup in (editor_menu(draft, RID, MAX_SECTIONS), prompt_menu("start", 9, RID), prompt_menu("end", 9, RID)):
            for _, data in buttons(markup):
                self.assertLessEqual(len(data.encode()), 64, data)

    # ------------------------------------------------------------ the editor
    async def test_open_shows_a_lean_editor(self):
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertEqual(labels(edit["markup"]), ["+ Add section", "Start: —", "End: —", "← Back"])
        self.assertIn("Length: 1:43:14", edit["text"])
        self.assertTrue(edit["text"].startswith("✄"))
        self.assertNotIn("✂", edit["text"])
        self.assertNotIn("Empty Start", edit["text"])
        for gone in ("Save", "Download", "Video", "MP3", "Opus", "Cancel"):
            self.assertFalse([l for l in labels(edit["markup"]) if gone in l], gone)

    async def test_photo_messages_are_edited_as_captions(self):
        self.assertEqual((await self.tap(f"dl|sec|open|{RID}", photo=True))["kind"], "caption")

    async def test_typing_a_start_time_fills_the_row_and_tidies_the_chat(self):
        prompt = await self.tap(f"dl|sec|start|0|{RID}")
        self.assertIn("Send the <b>start</b> time", prompt["text"])
        self.assertNotIn("for section", prompt["text"])             # one row: no number needed
        self.assertIn(USER, main.pending_section_input)

        await self.type_text("1:30:00")
        self.assertEqual(self.draft.rows[0].start, 5400)
        self.assertNotIn(USER, main.pending_section_input)
        self.assertIn("Start: 1:30:00", labels(self.bot.last["markup"]))
        self.assertEqual(len(self.deleted), 1)

    async def test_a_value_alone_is_a_section_no_save_step(self):
        await self.fill(0, "end", "2:00")                            # empty start = from the beginning
        self.assertEqual(self.draft.active(DURATION), [(0.0, 120.0)])
        await self.add_section(start="1:43:00")                      # empty end = to the end
        self.assertEqual(self.draft.active(DURATION), [(0.0, 120.0), (6180.0, 6194.0)])

    async def test_bad_input_keeps_waiting_and_says_why(self):
        await self.tap(f"dl|sec|start|0|{RID}")
        await self.type_text("banana")
        self.assertIn("⚠", self.bot.last["text"])
        self.assertIn("Send the <b>start</b> time", self.bot.last["text"])
        self.assertIn(USER, main.pending_section_input)
        self.assertIsNone(self.draft.rows[0].start)

    async def test_time_past_the_end_of_the_video_is_refused(self):
        await self.tap(f"dl|sec|end|0|{RID}")
        await self.type_text("2:00:00")
        self.assertIn("past the end of the video", self.bot.last["text"])
        self.assertIsNone(self.draft.rows[0].end)

    async def test_end_before_start_in_the_same_row_is_refused_at_once(self):
        await self.fill(0, "start", "10:00")
        await self.tap(f"dl|sec|end|0|{RID}")
        await self.type_text("5:00")
        self.assertIn("after the start", self.bot.last["text"])
        self.assertIsNone(self.draft.rows[0].end)
        await self.type_text("15:00")                                # still waiting: a retry works
        self.assertEqual(self.draft.rows[0].end, 900)

    async def test_error_text_echoing_user_input_is_html_escaped(self):
        await self.tap(f"dl|sec|start|0|{RID}")
        await self.type_text("<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.bot.last["text"])
        self.assertNotIn("<b>x</b>", self.bot.last["text"].split("⚠")[1])

    async def test_leave_empty_button_clears_a_field(self):
        await self.fill(0, "start", "5:00")
        await self.tap(f"dl|sec|start|0|{RID}")
        edit = await self.tap(f"dl|sec|clear|start|0|{RID}")
        self.assertIsNone(self.draft.rows[0].start)
        self.assertIn("Start: —", labels(edit["markup"]))

    # ---- adding sections
    async def test_add_section_is_refused_while_the_current_one_is_empty(self):
        await self.tap(f"dl|sec|open|{RID}")
        edit = await self.tap(f"dl|sec|add|{RID}")
        self.assertIn("Fill in the current section before adding another", edit["text"])
        self.assertEqual(len(self.draft.rows), 1)

    async def test_add_section_appends_a_row_once_the_first_is_filled(self):
        await self.fill(0, "end", "2:00")
        edit = await self.tap(f"dl|sec|add|{RID}")
        self.assertEqual(len(self.draft.rows), 2)
        self.assertEqual(labels(edit["markup"])[:6], ["+ Add section", "Start: —", "End: 2:00", "✕", "Start: —", "End: —"])

    async def test_typing_goes_to_the_row_that_was_tapped_and_the_prompt_names_it(self):
        await self.fill(0, "end", "2:00")
        await self.tap(f"dl|sec|add|{RID}")
        prompt = await self.tap(f"dl|sec|start|1|{RID}")
        self.assertIn("for section 2", prompt["text"])
        await self.type_text("10:00")
        self.assertEqual((self.draft.rows[1].start, self.draft.rows[0].start), (600, None))

    async def test_the_section_limit(self):
        for i in range(MAX_SECTIONS - 1):
            await self.add_section(end=f"{i + 1}:00")
        await self.add_section(end="30:00")
        self.assertEqual(len(self.draft.rows), MAX_SECTIONS)
        edit = await self.tap(f"dl|sec|add|{RID}")
        self.assertIn(str(MAX_SECTIONS), edit["text"])
        self.assertEqual(len(self.draft.rows), MAX_SECTIONS)

    # ---- removing sections
    async def test_a_lone_empty_row_has_no_remove_button_but_filled_ones_do(self):
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertNotIn("✕", labels(edit["markup"]))
        await self.fill(0, "end", "2:00")
        self.assertIn("✕", labels(self.bot.last["markup"]))

    async def test_remove_drops_that_row_and_the_editor_always_keeps_one(self):
        await self.add_section(end="2:00")
        await self.add_section(end="4:00")
        edit = await self.tap(f"dl|sec|del|0|{RID}")
        self.assertEqual([r.end for r in self.draft.rows], [240])
        self.assertIn("End: 4:00", labels(edit["markup"]))
        edit = await self.tap(f"dl|sec|del|0|{RID}")
        self.assertEqual(len(self.draft.rows), 1)
        self.assertTrue(self.draft.rows[0].is_empty())
        self.assertNotIn("✕", labels(edit["markup"]))

    async def test_a_stale_remove_button_is_ignored_with_a_notice(self):
        await self.tap(f"dl|sec|open|{RID}")
        edit = await self.tap(f"dl|sec|del|7|{RID}")
        self.assertIn("no longer exists", edit["text"])

    # ---- merged or separate
    async def test_merge_choice_only_appears_with_two_filled_sections(self):
        await self.add_section(end="2:00")
        self.assertNotIn("Merged into one", " ".join(labels(self.bot.last["markup"])))
        await self.tap(f"dl|sec|add|{RID}")                          # a second row, still empty
        self.assertNotIn("Merged into one", " ".join(labels(self.bot.last["markup"])))
        await self.fill(1, "end", "4:00")
        self.assertIn("● Separate clips", labels(self.bot.last["markup"]))
        edit = await self.tap(f"dl|sec|merge|on|{RID}")
        self.assertIn("● Merged into one", labels(edit["markup"]))
        self.assertTrue(self.draft.merge)

    # ------------------------------------------------------------ the quality menu
    async def test_back_returns_to_the_main_menu_whose_caption_lists_the_sections(self):
        await self.add_section(start="1:30:00", end="1:32:00")
        await self.add_section(start="10:00", end="11:00")
        edit = await self.tap(f"dl|sec|back|{RID}")
        self.assertIn(f"dl|video|best|{RID}", datas(edit["markup"]))
        self.assertIn("My long video", edit["text"])
        self.assertIn("✄ 2 sections — only these will be downloaded", edit["text"])
        self.assertIn("1. 1:30:00 – 1:32:00", edit["text"])
        self.assertIn("2. 10:00 – 11:00", edit["text"])
        self.assertIn("Separate clips", edit["text"])
        self.assertEqual(len(self.draft.rows), 2)                    # nothing was lost

    async def test_back_with_no_sections_leaves_the_caption_alone(self):
        await self.tap(f"dl|sec|open|{RID}")
        edit = await self.tap(f"dl|sec|back|{RID}")
        self.assertEqual(edit["text"], "My long video")
        self.assertNotIn("✄", edit["text"])

    async def test_more_options_marks_the_button_and_repeats_the_caption(self):
        await self.add_section(end="2:00")
        await self.add_section(end="4:00")
        await self.tap(f"dl|sec|back|{RID}")
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("✄ Sections (2) ✓", labels(edit["markup"]))
        self.assertIn("✄ 2 sections", edit["text"])
        edit = await self.tap(f"dl|backq|{RID}")
        self.assertIn("✄ 2 sections", edit["text"])
        self.assertIn(f"dl|video|best|{RID}", datas(edit["markup"]))

    async def test_a_long_list_is_shortened_in_the_caption(self):
        for i in range(6):
            await self.add_section(end=f"{i + 1}:00")
        edit = await self.tap(f"dl|sec|back|{RID}")
        self.assertIn("+2 more", edit["text"])
        self.assertNotIn("5. ", edit["text"])

    async def test_a_quality_button_downloads_just_the_sections_at_that_quality(self):
        await self.add_section(start="1:30:00", end="1:32:00")
        await self.add_section(start="10:00", end="11:00")
        await self.tap(f"dl|sec|merge|on|{RID}")
        await self.tap(f"dl|sec|back|{RID}")
        await self.tap(f"dl|video|480p|{RID}")

        job = self.jobs.enqueued[0]
        self.assertEqual(job["settings"]["sections"], [(5400.0, 5520.0), (600.0, 660.0)])   # order kept
        self.assertTrue(job["settings"]["sections_merge"])
        self.assertEqual((job["settings"]["mode"], job["settings"]["quality"]), ("video", "480p"))
        self.assertEqual(job["title"], "My long video")
        self.assertEqual(main.last_download[RID][2]["sections"], job["settings"]["sections"])   # Try again keeps them
        self.assertNotIn(RID, main.pending_sections)
        self.assertNotIn(RID, main.pending_probes)

    async def test_the_audio_buttons_clip_too(self):
        await self.add_section(end="1:00")
        await self.tap(f"dl|audio|mp3|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertEqual((settings["mode"], settings["audio_format"]), ("audio", "mp3"))
        self.assertEqual(settings["sections"], [(0.0, 60.0)])
        self.assertFalse(settings["sections_merge"])

    async def test_merge_is_ignored_for_a_single_section(self):
        await self.add_section(end="1:00")
        await self.tap(f"dl|sec|merge|on|{RID}")
        await self.tap(f"dl|video|best|{RID}")
        self.assertFalse(self.jobs.enqueued[0]["settings"]["sections_merge"])

    async def test_without_sections_a_download_is_the_whole_video_as_before(self):
        await self.tap(f"dl|sec|open|{RID}")                         # opened the editor, filled nothing
        await self.tap(f"dl|video|best|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertNotIn("sections", settings)
        self.assertNotIn("sections_merge", settings)

    async def test_normal_quality_pick_still_passes_the_preview_title(self):
        """Regression: the title used to be read after the probe state was dropped."""
        await self.tap(f"dl|video|best|{RID}")
        self.assertEqual(self.jobs.enqueued[0]["title"], "My long video")

    async def test_redo_shows_the_chosen_sections_too(self):
        await self.add_section(end="2:00")
        edit = await self.tap(f"dl|redo|{RID}")
        self.assertIn("✄ 1 section — only this will be downloaded", edit["text"])

    # ------------------------------------------------------------ chapters
    CHAPTERS = [("Intro", 0.0, 60.0), ("Main part", 60.0, 3000.0), ("Outro", 3000.0, float(DURATION))]

    def with_chapters(self, chapters=None):
        main.pending_probes[RID] = ProbeResult(ok=True, title="My long video", heights=[1080, 720, 480],
                                               has_audio=True, duration=DURATION,
                                               chapters=list(chapters or self.CHAPTERS))

    async def test_the_chapters_button_only_exists_when_the_video_has_chapters(self):
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertNotIn("Chapters", labels(edit["markup"]))
        edits_before = len(self.bot.edits)
        await self.tap(f"dl|sec|chap|0|{RID}")                      # a forged button: nothing to show, nothing happens
        self.assertEqual(len(self.bot.edits), edits_before)
        self.with_chapters()
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertEqual(labels(edit["markup"])[:2], ["+ Add section", "Chapters"])

    async def test_chapters_screen_lists_them_with_start_times(self):
        self.with_chapters()
        edit = await self.tap(f"dl|sec|chap|0|{RID}")
        self.assertIn("✄ <b>Chapters</b>", edit["text"])
        self.assertEqual(labels(edit["markup"]), ["0:00 · Intro", "1:00 · Main part", "50:00 · Outro", "← Back"])

    async def test_tapping_a_chapter_adds_it_as_a_section_and_tapping_again_removes_it(self):
        self.with_chapters()
        await self.tap(f"dl|sec|chap|0|{RID}")
        edit = await self.tap(f"dl|sec|ch|1|0|{RID}")
        self.assertIn("✓ 1:00 · Main part", labels(edit["markup"]))
        self.assertEqual([(r.start, r.end) for r in self.draft.rows], [(60.0, 3000.0)])     # filled the empty row, no blank left
        edit = await self.tap(f"dl|sec|ch|2|0|{RID}")
        self.assertEqual(len(self.draft.rows), 2)
        self.assertEqual(self.draft.active(DURATION), [(60.0, 3000.0), (3000.0, float(DURATION))])
        edit = await self.tap(f"dl|sec|ch|1|0|{RID}")
        self.assertNotIn("✓ 1:00 · Main part", labels(edit["markup"]))
        self.assertEqual(self.draft.active(DURATION), [(3000.0, float(DURATION))])

    async def test_chapters_and_typed_sections_mix_and_back_lists_them(self):
        self.with_chapters()
        await self.fill(0, "start", "10:00")
        await self.fill(0, "end", "11:00")
        await self.tap(f"dl|sec|chap|0|{RID}")
        await self.tap(f"dl|sec|ch|0|0|{RID}")
        edit = await self.tap(f"dl|sec|back|{RID}")
        self.assertIn("✄ 2 sections", edit["text"])
        self.assertIn("1. 10:00 – 11:00", edit["text"])
        self.assertIn("2. 0:00 – 1:00", edit["text"])

    async def test_chapters_are_paged_and_the_pages_navigate(self):
        self.with_chapters([(f"Chapter {i}", i * 100.0, i * 100.0 + 90) for i in range(20)])
        edit = await self.tap(f"dl|sec|chap|0|{RID}")
        self.assertIn("Page 1 of 3", edit["text"])
        self.assertEqual(len([l for l in labels(edit["markup"]) if "Chapter" in l]), 8)
        self.assertIn("Next ▶", labels(edit["markup"]))
        self.assertNotIn("◀ Prev", labels(edit["markup"]))
        edit = await self.tap(f"dl|sec|chap|2|{RID}")
        self.assertIn("Page 3 of 3", edit["text"])
        self.assertEqual(len([l for l in labels(edit["markup"]) if "Chapter" in l]), 4)
        self.assertIn("◀ Prev", labels(edit["markup"]))
        self.assertNotIn("Next ▶", labels(edit["markup"]))
        edit = await self.tap(f"dl|sec|ch|17|2|{RID}")                      # a chapter on the last page
        self.assertIn("✓ ", " ".join(labels(edit["markup"])))
        self.assertIn("Page 3 of 3", edit["text"])                         # stays on the page
        edit = await self.tap(f"dl|sec|chap|99|{RID}")                      # out-of-range page is clamped
        self.assertIn("Page 3 of 3", edit["text"])

    async def test_the_section_limit_shows_a_notice_on_the_chapters_screen(self):
        self.with_chapters([(f"C{i}", i * 100.0, i * 100.0 + 90) for i in range(MAX_SECTIONS + 2)])
        await self.tap(f"dl|sec|chap|0|{RID}")
        for i in range(MAX_SECTIONS):
            await self.tap(f"dl|sec|ch|{i}|0|{RID}")
        edit = await self.tap(f"dl|sec|ch|{MAX_SECTIONS}|1|{RID}")
        self.assertIn(str(MAX_SECTIONS), edit["text"])
        self.assertEqual(len(self.draft.rows), MAX_SECTIONS)

    async def test_a_stale_chapter_button_is_handled(self):
        self.with_chapters()
        edit = await self.tap(f"dl|sec|ch|9|0|{RID}")
        self.assertIn("no longer exists", edit["text"])

    async def test_chapter_buttons_fit_the_64_byte_limit_and_long_titles_are_cut(self):
        self.with_chapters([("x" * 80, 0.0, 60.0)] * 1)
        edit = await self.tap(f"dl|sec|chap|0|{RID}")
        for _, data in buttons(edit["markup"]):
            self.assertLessEqual(len(data.encode()), 64)
        self.assertLessEqual(len(labels(edit["markup"])[0]), 40)
        self.assertTrue(labels(edit["markup"])[0].endswith("…"))

    # ------------------------------------------------------------ one MP3 per chapter
    async def test_the_split_button_appears_in_more_options_when_there_are_chapters(self):
        self.with_chapters()
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("♪ MP3 · 3 tracks", labels(edit["markup"]))
        self.assertIn(f"dl|audio|mp3split|{RID}", datas(edit["markup"]))

    async def test_the_split_button_is_hidden_without_enough_chapters_or_with_too_many(self):
        for chapters in ([], [("Only", 0.0, 60.0)], [(f"C{i}", i * 10.0, i * 10.0 + 9) for i in range(51)]):
            with self.subTest(count=len(chapters)):
                if chapters:
                    self.with_chapters(chapters)
                else:
                    main.pending_probes[RID] = ProbeResult(ok=True, heights=[720], has_audio=True, duration=DURATION)
                edit = await self.tap(f"dl|moreq|{RID}")
                self.assertFalse([l for l in labels(edit["markup"]) if "tracks" in l])

    async def test_the_split_button_is_hidden_while_sections_are_chosen(self):
        self.with_chapters()
        await self.add_section(end="2:00")
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertFalse([l for l in labels(edit["markup"]) if "tracks" in l])

    async def test_the_split_button_also_sits_on_an_audio_only_sources_menu(self):
        main.pending_probes[RID] = ProbeResult(ok=True, heights=[], has_audio=True, duration=DURATION,
                                               chapters=list(self.CHAPTERS))
        self.assertIn("♪ MP3 · 3 tracks", labels(main._main_menu_for(RID, main.pending_probes[RID], USER)))

    async def test_the_split_button_shows_an_estimated_size(self):
        main.pending_probes[RID] = ProbeResult(ok=True, heights=[720], has_audio=True, duration=DURATION,
                                               chapters=list(self.CHAPTERS), sizes={"mp3": 86_000_000})
        self.assertIn("♪ MP3 · 3 tracks ~86MB", labels(main._more_menu_for(RID, main.pending_probes[RID], USER)))

    async def test_tapping_it_downloads_mp3_and_asks_for_the_split(self):
        self.with_chapters()
        await self.tap(f"dl|audio|mp3split|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertEqual((settings["mode"], settings["audio_format"], settings["split_chapters"]), ("audio", "mp3", True))
        self.assertNotIn("sections", settings)

    async def test_an_ordinary_mp3_button_does_not_split(self):
        self.with_chapters()
        await self.tap(f"dl|audio|mp3|{RID}")
        self.assertNotIn("split_chapters", self.jobs.enqueued[0]["settings"])

    async def test_a_stale_split_button_does_not_override_chosen_sections(self):
        self.with_chapters()
        await self.add_section(end="2:00")
        await self.tap(f"dl|audio|mp3split|{RID}")                  # a button from before the sections were chosen
        settings = self.jobs.enqueued[0]["settings"]
        self.assertEqual(settings["sections"], [(0.0, 120.0)])
        self.assertNotIn("split_chapters", settings)

    # ------------------------------------------------------------ subtitles
    TRACKS = [SubTrack("fa", "Persian"), SubTrack("en", "English"), SubTrack("de", "German"), SubTrack("ar", "Arabic", True)]

    def with_subs(self, tracks=None):
        main.pending_probes[RID] = ProbeResult(ok=True, title="My long video", heights=[1080, 720, 480],
                                               has_audio=True, duration=DURATION, subtitles=list(tracks or self.TRACKS))

    async def test_the_subtitles_button_lives_in_more_options_only_when_there_are_tracks(self):
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertFalse([l for l in labels(edit["markup"]) if "Subtitles" in l])
        self.with_subs()
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("◧ Subtitles", labels(edit["markup"]))
        self.assertIn(f"dl|sub|open|0|{RID}", datas(edit["markup"]))
        self.assertNotIn("◧ Subtitles", labels((await self.tap(f"dl|backq|{RID}"))["markup"]))     # not on the main menu

    async def test_opening_it_lists_persian_and_english_first_and_nothing_is_chosen(self):
        self.with_subs()
        edit = await self.tap(f"dl|sub|open|0|{RID}")
        names = [l for l in labels(edit["markup"]) if l[0] in "●○" and "Embedded" not in l and ".srt" not in l and "Both" not in l]
        self.assertEqual(names, ["○ Persian", "○ English", "○ German", "○ Arabic · auto"])
        self.assertIn("Selected: none", edit["text"])

    async def test_choosing_languages_and_a_mode_and_clearing(self):
        self.with_subs()
        await self.tap(f"dl|sub|open|0|{RID}")
        await self.tap(f"dl|sub|t|0|0|{RID}")
        edit = await self.tap(f"dl|sub|t|1|0|{RID}")
        self.assertIn("● Persian", labels(edit["markup"]))
        self.assertIn("Selected: Persian, English · embedded track", edit["text"])
        edit = await self.tap(f"dl|sub|m|both|0|{RID}")
        self.assertIn("● Both", labels(edit["markup"]))
        self.assertIn("embedded + .srt file", edit["text"])
        edit = await self.tap(f"dl|sub|t|0|0|{RID}")                      # tapping again removes
        self.assertIn("Selected: English", edit["text"])
        edit = await self.tap(f"dl|sub|clr|{RID}")
        self.assertIn("Selected: none", edit["text"])

    async def test_the_language_limit_and_stale_buttons(self):
        self.with_subs([SubTrack(f"l{i}", f"Lang {i}") for i in range(6)])
        for i in range(4):
            await self.tap(f"dl|sub|t|{i}|0|{RID}")
        edit = await self.tap(f"dl|sub|t|4|0|{RID}")
        self.assertIn("up to 4 languages", edit["text"])
        edit = await self.tap(f"dl|sub|t|99|0|{RID}")
        self.assertIn("no longer listed", edit["text"])
        edit = await self.tap(f"dl|sub|m|burn|0|{RID}")                    # burned-in isn't offered: refused
        self.assertIn("Unknown option", edit["text"])
        self.assertEqual(main.pending_subs[RID].mode, "embed")

    async def test_the_languages_are_paged(self):
        self.with_subs([SubTrack(f"l{i}", f"Lang {i}") for i in range(20)])
        edit = await self.tap(f"dl|sub|open|0|{RID}")
        self.assertIn("Page 1 of 3", edit["text"])
        edit = await self.tap(f"dl|sub|open|2|{RID}")
        self.assertIn("Page 3 of 3", edit["text"])
        edit = await self.tap(f"dl|sub|t|17|2|{RID}")
        self.assertIn("● Lang 17", labels(edit["markup"]))
        self.assertIn("Page 3 of 3", edit["text"])                         # stays on the page

    async def test_back_returns_to_more_options_with_the_choice_in_the_caption_and_on_the_button(self):
        self.with_subs()
        await self.tap(f"dl|sub|t|0|0|{RID}")
        await self.tap(f"dl|sub|t|1|0|{RID}")
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("◧ Subtitles (2) ✓", labels(edit["markup"]))
        self.assertIn("◧ Subtitles: Persian, English · embedded track", edit["text"])
        edit = await self.tap(f"dl|backq|{RID}")
        self.assertIn("◧ Subtitles: Persian, English", edit["text"])       # the main menu says so too

    async def test_a_quality_button_downloads_with_the_subtitles(self):
        self.with_subs()
        await self.tap(f"dl|sub|t|0|0|{RID}")
        await self.tap(f"dl|sub|m|file|0|{RID}")
        await self.tap(f"dl|video|720p|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertEqual((settings["sub_langs"], settings["sub_mode"]), (["fa"], "file"))
        self.assertEqual(settings["quality"], "720p")
        self.assertNotIn(RID, main.pending_subs)
        self.assertEqual(main.last_download[RID][2]["sub_langs"], ["fa"])  # Try again keeps them

    async def test_audio_downloads_ignore_subtitles(self):
        self.with_subs()
        await self.tap(f"dl|sub|t|0|0|{RID}")
        await self.tap(f"dl|audio|mp3|{RID}")
        self.assertNotIn("sub_langs", self.jobs.enqueued[0]["settings"])

    async def test_nothing_chosen_means_no_subtitle_settings(self):
        self.with_subs()
        await self.tap(f"dl|sub|open|0|{RID}")                             # looked, chose nothing
        await self.tap(f"dl|video|best|{RID}")
        self.assertNotIn("sub_langs", self.jobs.enqueued[0]["settings"])

    async def test_time_ranges_win_over_subtitles_and_the_caption_says_so(self):
        self.with_subs()
        await self.tap(f"dl|sub|t|0|0|{RID}")
        await self.add_section(end="2:00")
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertFalse([l for l in labels(edit["markup"]) if "Subtitles" in l])      # the button is hidden meanwhile
        self.assertIn("◧ Subtitles are skipped for time ranges.", edit["text"])
        self.assertNotIn("◧ Subtitles: Persian", edit["text"])
        await self.tap(f"dl|video|720p|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertEqual(settings["sections"], [(0.0, 120.0)])
        self.assertNotIn("sub_langs", settings)

    async def test_removing_the_sections_brings_the_subtitles_back(self):
        self.with_subs()
        await self.tap(f"dl|sub|t|0|0|{RID}")
        await self.add_section(end="2:00")
        await self.tap(f"dl|sec|del|0|{RID}")
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("◧ Subtitles (1) ✓", labels(edit["markup"]))
        self.assertIn("◧ Subtitles: Persian", edit["text"])

    async def test_expired_link_and_other_peoples_buttons(self):
        self.with_subs()
        main.pending_links.clear()
        self.assertIn("expired", (await self.tap(f"dl|sub|open|0|{RID}"))["text"])
        main.pending_links[RID] = (999, URL)
        self.assertIn("expired", (await self.tap(f"dl|sub|open|0|{RID}"))["text"])
        self.assertNotIn(RID, main.pending_subs)

    async def test_the_caption_escapes_language_names(self):
        self.with_subs([SubTrack("x", "<b>Odd</b>")])
        await self.tap(f"dl|sub|t|0|0|{RID}")
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("&lt;b&gt;Odd&lt;/b&gt;", edit["text"])

    # ------------------------------------------------------------ sites where gallery-dl comes first
    async def test_a_video_chosen_on_x_or_pinterest_is_downloaded_with_yt_dlp_first(self):
        for url in ("https://x.com/u/status/1", "https://www.pinterest.com/pin/1/"):
            with self.subTest(url=url):
                self.jobs.enqueued.clear()
                main.pending_links[RID] = (USER, url)
                main.pending_probes[RID] = ProbeResult(ok=True, heights=[720], has_audio=True, duration=60)
                await self.tap(f"dl|video|best|{RID}")
                self.assertTrue(self.jobs.enqueued[0]["settings"]["prefer_ytdlp"])

    async def test_sections_on_x_prefer_yt_dlp_as_well(self):
        main.pending_links[RID] = (USER, "https://x.com/u/status/1")
        await self.add_section(end="0:30")
        await self.tap(f"dl|video|best|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertTrue(settings["prefer_ytdlp"])
        self.assertEqual(settings["sections"], [(0.0, 30.0)])

    async def test_sites_that_already_try_yt_dlp_first_are_left_alone(self):
        await self.tap(f"dl|video|best|{RID}")                    # the default test link is YouTube
        self.assertNotIn("prefer_ytdlp", self.jobs.enqueued[0]["settings"])

    # ------------------------------------------------------------ ?t= links
    def test_a_start_time_in_the_link_prefills_a_section_to_the_end(self):
        main._prefill_start_time(RID, "https://youtu.be/abc?t=1h30m", main.pending_probes[RID])
        self.assertEqual(main.pending_sections[RID].active(DURATION), [(5400.0, float(DURATION))])
        self.assertIn("✄ 1 section", main._quality_caption(RID, main.pending_probes[RID]))
        self.assertIn("1:30:00 – 1:43:14", main._quality_caption(RID, main.pending_probes[RID]))

    def test_no_prefill_for_plain_links_other_sites_or_times_past_the_end(self):
        probe = main.pending_probes[RID]
        for url in ("https://youtu.be/abc", "https://vimeo.com/1?t=90", "https://youtu.be/abc?t=0",
                    "https://youtu.be/abc?t=9999999", "https://youtu.be/abc?t=junk"):
            with self.subTest(url=url):
                main._prefill_start_time(RID, url, probe)
                self.assertNotIn(RID, main.pending_sections)
        main._prefill_start_time(RID, "https://youtu.be/abc?t=90", ProbeResult(ok=True, duration=None))
        self.assertNotIn(RID, main.pending_sections)

    async def test_a_prefilled_link_downloads_from_that_point_and_can_be_cleared(self):
        main._prefill_start_time(RID, "https://youtu.be/abc?t=5400", main.pending_probes[RID])
        edit = await self.tap(f"dl|moreq|{RID}")
        self.assertIn("✄ Sections (1) ✓", labels(edit["markup"]))
        await self.tap(f"dl|video|720p|{RID}")
        self.assertEqual(self.jobs.enqueued[0]["settings"]["sections"], [(5400.0, float(DURATION))])

    async def test_removing_the_prefilled_section_gives_back_the_whole_video(self):
        main._prefill_start_time(RID, "https://youtu.be/abc?t=5400", main.pending_probes[RID])
        await self.tap(f"dl|sec|del|0|{RID}")
        edit = await self.tap(f"dl|sec|back|{RID}")
        self.assertEqual(edit["text"], "My long video")
        await self.tap(f"dl|video|best|{RID}")
        self.assertNotIn("sections", self.jobs.enqueued[0]["settings"])

    # ------------------------------------------------------------ housekeeping
    async def test_expired_link_says_so_instead_of_crashing(self):
        main.pending_links.clear()
        self.assertIn("expired", (await self.tap(f"dl|sec|open|{RID}"))["text"])

    async def test_someone_elses_buttons_do_nothing(self):
        main.pending_links[RID] = (999, URL)
        self.assertIn("expired", (await self.tap(f"dl|sec|open|{RID}"))["text"])
        self.assertNotIn(RID, main.pending_sections)

    async def test_not_modified_errors_are_swallowed_but_others_are_not(self):
        await self.tap(f"dl|sec|open|{RID}")
        self.bot.fail_with = BadRequest("Bad Request: message is not modified")
        await self.tap(f"dl|sec|open|{RID}")
        self.bot.fail_with = BadRequest("Bad Request: chat not found")
        with self.assertRaises(BadRequest):
            await self.tap(f"dl|sec|open|{RID}")

    async def test_any_other_button_abandons_a_half_typed_time(self):
        await self.tap(f"dl|sec|start|0|{RID}")
        self.assertIn(USER, main.pending_section_input)
        await self.tap(f"dl|backq|{RID}")
        self.assertNotIn(USER, main.pending_section_input)

    async def test_pasting_a_new_link_while_waiting_moves_on(self):
        async def deny(*a, **k):
            return False                                              # stop link_handler right after the check
        main.gate = deny
        await self.tap(f"dl|sec|start|0|{RID}")
        await self.type_text("https://youtu.be/other")
        self.assertNotIn(USER, main.pending_section_input)

    async def test_a_forgotten_prompt_expires(self):
        await self.tap(f"dl|sec|start|0|{RID}")
        main.pending_section_input[USER]["at"] = time.time() - main.SECTION_INPUT_TTL_SECONDS - 1
        edits_before = len(self.bot.edits)
        await self.type_text("1:00")
        self.assertNotIn(USER, main.pending_section_input)
        self.assertEqual(len(self.bot.edits), edits_before)
        self.assertIsNone(self.draft.rows[0].start)

    async def test_typed_input_after_the_link_expired(self):
        await self.tap(f"dl|sec|start|0|{RID}")
        main.pending_links.clear()
        await self.type_text("1:00")
        self.assertIn("expired", self.bot.last["text"])
        self.assertNotIn(USER, main.pending_section_input)


if __name__ == "__main__":
    unittest.main()
