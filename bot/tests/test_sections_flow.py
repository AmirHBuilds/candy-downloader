"""
The clip-sections editor end to end: real handlers in main.py, driven with fake
Telegram objects (a bot that records edits, a query, a message).
"""
import time
import unittest

from tests import _env  # noqa: F401  (must come first)

from telegram.error import BadRequest  # noqa: E402  (the stub)

import main  # noqa: E402
from downloader.probe import ProbeResult  # noqa: E402
from downloader.sections import SectionDraft  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import quick_menu  # noqa: E402

USER, RID, URL = 42, "rid0000001", "https://www.youtube.com/watch?v=abc"
DURATION = 6194   # 1:43:14


class FakeBot:
    def __init__(self):
        self.edits: list[dict] = []
        self.fail_with: Exception | None = None

    async def _record(self, kind, text, kw):
        if self.fail_with:
            raise self.fail_with
        self.edits.append({"kind": kind, "text": text, "markup": kw.get("reply_markup")})

    async def edit_message_text(self, text, **kw):
        await self._record("text", text, kw)

    async def edit_message_caption(self, caption, **kw):
        await self._record("caption", caption, kw)

    @property
    def last(self):
        return self.edits[-1]


class FakeMessage:
    def __init__(self, text="", photo=False):
        self.chat_id, self.message_id, self.text = 7, 99, text
        self.photo = [object()] if photo else []
        self.caption = None
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, photo=False):
        self.data, self.message = data, FakeMessage(photo=photo)

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, **kw):
        pass

    async def edit_message_caption(self, **kw):
        pass

    async def edit_message_reply_markup(self, **kw):
        pass


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


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot, self.jobs = FakeBot(), FakeJobManager()
        for name, value in [("pending_links", {}), ("pending_probes", {}), ("pending_sections", {}),
                            ("pending_section_input", {}), ("last_download", {})]:
            setattr(main, name, value)
        main.pending_links[RID] = (USER, URL)
        main.pending_probes[RID] = ProbeResult(ok=True, title="My long video", heights=[1080, 720],
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
        await main.link_callback(FakeUpdate(self.bot, FakeQuery(data, photo)), FakeContext(self.bot))
        return self.bot.edits[-1] if self.bot.edits else None

    async def type_text(self, text):
        await main.link_handler(FakeUpdate(self.bot, text=text), FakeContext(self.bot))

    async def set_field(self, field, text):
        await self.tap(f"dl|sec|{field}|{RID}")
        await self.type_text(text)

    # -- entry point
    def test_button_only_offered_when_the_length_is_known(self):
        known = ProbeResult(ok=True, heights=[720], has_audio=True, duration=100)
        unknown = ProbeResult(ok=True, heights=[720], has_audio=True, duration=None)
        self.assertIn(f"dl|sec|open|{RID}", [d for _, d in buttons(quick_menu.extended_video_menu(known, RID))])
        self.assertNotIn(f"dl|sec|open|{RID}", [d for _, d in buttons(quick_menu.extended_video_menu(unknown, RID))])
        audio_only = ProbeResult(ok=True, heights=[], has_audio=True, duration=100)
        self.assertIn(f"dl|sec|open|{RID}", [d for _, d in buttons(quick_menu.quality_menu(audio_only, RID))])

    def test_every_callback_fits_telegrams_64_byte_limit(self):
        draft = SectionDraft(sections=[(i * 100.0, i * 100.0 + 50) for i in range(10)])
        from ui.section_menu import editor_menu, prompt_menu
        for markup in (editor_menu(draft, RID, True), prompt_menu("start", RID), prompt_menu("end", RID)):
            for _, data in buttons(markup):
                self.assertLessEqual(len(data.encode()), 64, data)

    # -- editor
    async def test_open_shows_the_editor_with_the_video_length(self):
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertIn("Length: 1:43:14", edit["text"])
        self.assertIn("No sections saved yet.", edit["text"])
        labels = [t for t, _ in buttons(edit["markup"])]
        self.assertIn("Start: —", labels)
        self.assertIn("End: —", labels)
        self.assertIn("● Video", labels)

    async def test_photo_messages_are_edited_as_captions(self):
        edit = await self.tap(f"dl|sec|open|{RID}", photo=True)
        self.assertEqual(edit["kind"], "caption")

    async def test_typing_a_start_time_fills_the_draft_and_tidies_the_chat(self):
        prompt = await self.tap(f"dl|sec|start|{RID}")
        self.assertIn("Send the <b>start</b> time", prompt["text"])
        self.assertIn(USER, main.pending_section_input)

        await self.type_text("1:30:00")
        self.assertEqual(main.pending_sections[RID].start, 5400)
        self.assertNotIn(USER, main.pending_section_input)
        self.assertIn("Start: 1:30:00", self.bot.last["text"])
        self.assertEqual(len(self.deleted), 1)                 # the person's message was removed

    async def test_bad_timestamp_keeps_waiting_and_says_why(self):
        await self.tap(f"dl|sec|start|{RID}")
        await self.type_text("banana")
        self.assertIn("⚠", self.bot.last["text"])
        self.assertIn("Send the <b>start</b> time", self.bot.last["text"])
        self.assertIn(USER, main.pending_section_input)         # still armed
        self.assertIsNone(main.pending_sections[RID].start)

    async def test_time_past_the_end_of_the_video_is_refused(self):
        await self.tap(f"dl|sec|end|{RID}")
        await self.type_text("2:00:00")
        self.assertIn("past the end of the video", self.bot.last["text"])
        self.assertIsNone(main.pending_sections[RID].end)

    async def test_error_text_echoing_user_input_is_html_escaped(self):
        await self.tap(f"dl|sec|start|{RID}")
        await self.type_text("<b>x</b>")
        self.assertNotIn("<b>x</b>", self.bot.last["text"].split("⚠")[1])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.bot.last["text"])

    async def test_saving_with_both_fields_empty_shows_an_error_and_saves_nothing(self):
        await self.tap(f"dl|sec|open|{RID}")
        edit = await self.tap(f"dl|sec|save|{RID}")
        self.assertIn("can't both be empty", edit["text"])
        self.assertEqual(main.pending_sections[RID].sections, [])

    async def test_one_empty_field_is_accepted(self):
        await self.set_field("end", "2:00")                     # empty start = from the beginning
        edit = await self.tap(f"dl|sec|save|{RID}")
        self.assertEqual(main.pending_sections[RID].sections, [(0.0, 120.0)])
        self.assertIn("1. 0:00 – 2:00", edit["text"])
        self.assertIn("Start: —   End: —", edit["text"])         # draft reset for the next one

        await self.set_field("start", "1:43:00")                # empty end = to the end
        await self.tap(f"dl|sec|save|{RID}")
        self.assertEqual(main.pending_sections[RID].sections[1], (6180.0, 6194.0))

    async def test_leave_empty_button_clears_a_field(self):
        await self.set_field("start", "5:00")
        await self.tap(f"dl|sec|start|{RID}")
        edit = await self.tap(f"dl|sec|clear|start|{RID}")
        self.assertIsNone(main.pending_sections[RID].start)
        self.assertIn("Start: —", edit["text"])

    async def test_remove_and_duplicate_and_limit(self):
        await self.set_field("start", "1:00")
        await self.set_field("end", "2:00")
        await self.tap(f"dl|sec|save|{RID}")
        await self.set_field("start", "1:00")
        await self.set_field("end", "2:00")
        edit = await self.tap(f"dl|sec|save|{RID}")
        self.assertIn("already in the list", edit["text"])
        edit = await self.tap(f"dl|sec|del|1|{RID}")
        self.assertEqual(main.pending_sections[RID].sections, [])
        self.assertIn("No sections saved yet.", edit["text"])
        await self.tap(f"dl|sec|del|9|{RID}")                    # stale button: ignored, no crash

    async def test_merge_choice_only_appears_with_two_or_more_sections(self):
        for start, end in [("1:00", "2:00"), ("3:00", "4:00")]:
            await self.set_field("start", start)
            await self.set_field("end", end)
            edit = await self.tap(f"dl|sec|save|{RID}")
        labels = [t for t, _ in buttons(edit["markup"])]
        self.assertIn("● Separate clips", labels)
        edit = await self.tap(f"dl|sec|merge|on|{RID}")
        self.assertIn("● Merged into one", [t for t, _ in buttons(edit["markup"])])

    async def test_video_format_not_offered_for_audio_only_sources(self):
        main.pending_probes[RID] = ProbeResult(ok=True, title="Podcast", heights=[], has_audio=True, duration=3000)
        edit = await self.tap(f"dl|sec|open|{RID}")
        labels = [t for t, _ in buttons(edit["markup"])]
        self.assertNotIn("● Video", labels)
        self.assertNotIn("○ Video", labels)
        self.assertEqual(main.pending_sections[RID].fmt, "mp3")
        await self.tap(f"dl|sec|fmt|video|{RID}")                # forged/stale button: ignored
        self.assertEqual(main.pending_sections[RID].fmt, "mp3")

    async def test_not_modified_errors_are_swallowed(self):
        await self.tap(f"dl|sec|open|{RID}")
        self.bot.fail_with = BadRequest("Bad Request: message is not modified")
        await self.tap(f"dl|sec|fmt|video|{RID}")                # would raise if not handled
        self.bot.fail_with = BadRequest("Bad Request: chat not found")
        with self.assertRaises(BadRequest):
            await self.tap(f"dl|sec|fmt|mp3|{RID}")

    # -- starting the download
    async def test_download_starts_a_clip_job_with_the_saved_sections(self):
        for start, end in [("1:30:00", "1:32:00"), ("10:00", "11:00")]:
            await self.set_field("start", start)
            await self.set_field("end", end)
            await self.tap(f"dl|sec|save|{RID}")
        await self.tap(f"dl|sec|merge|on|{RID}")
        await self.tap(f"dl|sec|fmt|mp3|{RID}")
        edit = await self.tap(f"dl|sec|go|{RID}")

        self.assertEqual(len(self.jobs.enqueued), 1)
        job = self.jobs.enqueued[0]
        self.assertEqual(job["settings"]["sections"], [(5400.0, 5520.0), (600.0, 660.0)])   # order kept
        self.assertTrue(job["settings"]["sections_merge"])
        self.assertEqual((job["settings"]["mode"], job["settings"]["audio_format"]), ("audio", "mp3"))
        self.assertEqual(job["title"], "My long video")           # regression: was lost when state was popped first
        self.assertEqual(main.last_download[RID][2]["sections"], job["settings"]["sections"])  # Try again reuses it
        self.assertNotIn(RID, main.pending_sections)
        self.assertNotIn(RID, main.pending_probes)
        self.assertIn("Queued", edit["text"])

    async def test_video_clips_use_best_quality(self):
        await self.set_field("end", "1:00")
        await self.tap(f"dl|sec|save|{RID}")
        await self.tap(f"dl|sec|go|{RID}")
        settings = self.jobs.enqueued[0]["settings"]
        self.assertEqual((settings["mode"], settings["quality"]), ("video", "best"))
        self.assertFalse(settings["sections_merge"])

    async def test_normal_quality_pick_still_passes_the_preview_title(self):
        """Regression: the title used to be read after the probe state was dropped, so
        /history never got it for failed or cancelled downloads."""
        await self.tap(f"dl|video|best|{RID}")
        self.assertEqual(self.jobs.enqueued[0]["title"], "My long video")
        self.assertNotIn("sections", self.jobs.enqueued[0]["settings"])

    async def test_download_is_blocked_until_something_is_saved(self):
        await self.tap(f"dl|sec|open|{RID}")
        edit = await self.tap(f"dl|sec|go|{RID}")
        self.assertIn("Add at least one section", edit["text"])
        self.assertEqual(self.jobs.enqueued, [])

    async def test_download_is_blocked_while_a_section_is_unsaved(self):
        await self.set_field("start", "1:00")
        await self.set_field("end", "2:00")
        await self.tap(f"dl|sec|save|{RID}")
        await self.set_field("start", "5:00")                    # typed but not saved
        edit = await self.tap(f"dl|sec|go|{RID}")
        self.assertIn("unsaved section", edit["text"])
        self.assertEqual(self.jobs.enqueued, [])

    # -- housekeeping
    async def test_expired_link_says_so_instead_of_crashing(self):
        main.pending_links.clear()
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertIn("expired", edit["text"])

    async def test_someone_elses_buttons_do_nothing(self):
        main.pending_links[RID] = (999, URL)
        edit = await self.tap(f"dl|sec|open|{RID}")
        self.assertIn("expired", edit["text"])
        self.assertNotIn(RID, main.pending_sections)

    async def test_back_returns_to_the_quality_menu_and_keeps_the_draft(self):
        await self.set_field("start", "1:00")
        edit = await self.tap(f"dl|sec|back|{RID}")
        self.assertIn(f"dl|video|best|{RID}", [d for _, d in buttons(edit["markup"])])
        self.assertEqual(main.pending_sections[RID].start, 60)

    async def test_any_other_button_abandons_a_half_typed_time(self):
        await self.tap(f"dl|sec|start|{RID}")
        self.assertIn(USER, main.pending_section_input)
        await self.tap(f"dl|backq|{RID}")
        self.assertNotIn(USER, main.pending_section_input)

    async def test_pasting_a_new_link_while_waiting_moves_on(self):
        async def deny(*a, **k):
            return False                                          # stop link_handler right after the check
        main.gate = deny
        await self.tap(f"dl|sec|start|{RID}")
        await self.type_text("https://youtu.be/other")
        self.assertNotIn(USER, main.pending_section_input)

    async def test_a_forgotten_prompt_expires(self):
        await self.tap(f"dl|sec|start|{RID}")
        main.pending_section_input[USER]["at"] = time.time() - main.SECTION_INPUT_TTL_SECONDS - 1
        edits_before = len(self.bot.edits)
        await self.type_text("1:00")                              # would have been swallowed as a timestamp
        self.assertNotIn(USER, main.pending_section_input)
        self.assertEqual(len(self.bot.edits), edits_before)
        self.assertIsNone(main.pending_sections[RID].start)

    async def test_typed_input_after_the_link_expired(self):
        await self.tap(f"dl|sec|start|{RID}")
        main.pending_links.clear()
        await self.type_text("1:00")
        self.assertIn("expired", self.bot.last["text"])
        self.assertNotIn(USER, main.pending_section_input)


if __name__ == "__main__":
    unittest.main()
