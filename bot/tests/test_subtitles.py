"""Soft subtitles: which tracks exist, the picker screen, yt-dlp options, and delivery."""
import asyncio
import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
from downloader import subtitles as subs, ytdlp_handler  # noqa: E402
from downloader.subtitles import SubChoice, SubTrack, SubtitleError, available_tracks, summary  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import subtitle_menu  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
RID = "rid0000005"


def labels(markup):
    return [b.text for b in markup.buttons()]


def datas(markup):
    return [b.callback_data for b in markup.buttons()]


# ============================================================ which tracks, in what order
class Tracks(unittest.TestCase):
    INFO = {
        "subtitles": {"de": [{"name": "German"}], "en": [{}], "live_chat": [{}], "fa": [{"name": "Persian"}], "empty": []},
        "automatic_captions": {"en": [{}], "en-orig": [{"name": "English (Original)"}], "ar": [{}], "fa": [{}],
                               "zh-Hans": [{}], "xx-YY": [{}]},
    }

    def test_persian_then_english_then_the_rest_human_made_before_automatic(self):
        got = [(t.code, t.auto) for t in available_tracks(self.INFO)]
        self.assertEqual(got[:3], [("fa", False), ("en", False), ("en-orig", True)])
        self.assertEqual(got[3], ("de", False))                        # other human-made before any automatic one
        self.assertTrue(all(auto for _, auto in got[4:]))
        self.assertEqual([c for c, _ in got[4:]], ["ar", "zh-Hans", "xx-YY"])      # automatic ones by name

    def test_one_track_per_language_a_human_one_wins_over_an_automatic_one(self):
        tracks = available_tracks(self.INFO)
        self.assertEqual([t.code for t in tracks].count("en"), 1)
        self.assertFalse(next(t for t in tracks if t.code == "en").auto)
        self.assertFalse(next(t for t in tracks if t.code == "fa").auto)

    def test_live_chat_and_empty_tracks_are_not_subtitles(self):
        codes = [t.code for t in available_tracks(self.INFO)]
        self.assertNotIn("live_chat", codes)
        self.assertNotIn("empty", codes)

    def test_regional_variants_count_as_their_language_for_ordering(self):
        info = {"subtitles": {"es": [{}], "en-US": [{}], "fa-IR": [{}], "en-GB": [{}]}}
        self.assertEqual([t.code for t in available_tracks(info)], ["fa-IR", "en-GB", "en-US", "es"])

    def test_names(self):
        self.assertEqual(subs.language_name("fa"), "Persian")
        self.assertEqual(subs.language_name("fa", "Farsi"), "Farsi")            # the site's own name wins
        self.assertEqual(subs.language_name("pt-BR"), "Portuguese (pt-BR)")
        self.assertEqual(subs.language_name("qq"), "qq")

    def test_nothing_offered(self):
        self.assertEqual(available_tracks({}), [])
        self.assertEqual(available_tracks({"subtitles": None, "automatic_captions": None}), [])


class Choice(unittest.TestCase):
    def test_toggle_limit_and_settings(self):
        choice = SubChoice()
        for code in ("fa", "en", "de", "ar"):
            self.assertTrue(choice.toggle(code))
        with self.assertRaisesRegex(SubtitleError, "up to 4"):
            choice.toggle("tr")
        self.assertFalse(choice.toggle("de"))                                    # tapping again removes
        self.assertTrue(choice.toggle("tr"))
        self.assertEqual(choice.settings(), {"sub_langs": ["fa", "en", "ar", "tr"], "sub_mode": "embed"})

    def test_modes(self):
        choice = SubChoice()
        for mode in ("file", "both", "embed"):
            choice.set_mode(mode)
            self.assertEqual(choice.mode, mode)
        with self.assertRaises(SubtitleError):
            choice.set_mode("burn")                                              # burned-in is not offered (yet)

    def test_summary(self):
        tracks = [SubTrack("fa", "Persian"), SubTrack("en", "English")]
        choice = SubChoice(["fa", "en"], "both")
        self.assertEqual(summary(choice, tracks), "Persian, English · embedded + .srt file")
        self.assertEqual(summary(SubChoice(["zz"], "file"), tracks), "zz · separate .srt file")


# ============================================================ the screen
def many_tracks(n):
    return [SubTrack(f"l{i}", f"Language {i}", auto=i > 2) for i in range(n)]


class Screen(unittest.TestCase):
    def test_a_page_shows_eight_languages_with_marks_and_the_auto_label(self):
        tracks = many_tracks(20)
        choice = SubChoice(["l1"])
        menu = subtitle_menu.subtitles_menu(choice, tracks, 0, RID)
        rows = [l for l in labels(menu) if "Language" in l]
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0], "○ Language 0")
        self.assertEqual(rows[1], "● Language 1")
        self.assertEqual(rows[3], "○ Language 3 · auto")

    def test_mode_row_marks_the_current_choice(self):
        menu = subtitle_menu.subtitles_menu(SubChoice(mode="file"), many_tracks(3), 0, RID)
        self.assertEqual(labels(menu)[:3], ["○ Embedded", "● .srt file", "○ Both"])

    def test_paging(self):
        tracks = many_tracks(20)
        self.assertEqual(subs.page_count(tracks), 3)
        first = labels(subtitle_menu.subtitles_menu(SubChoice(), tracks, 0, RID))
        self.assertIn("Next ▶", first)
        self.assertNotIn("◀ Prev", first)
        last = subtitle_menu.subtitles_menu(SubChoice(), tracks, 99, RID)           # clamped
        self.assertEqual(len([l for l in labels(last) if "Language" in l]), 4)
        self.assertNotIn("Next ▶", labels(last))
        self.assertIn("dl|sub|open|1|" + RID, datas(last))

    def test_clear_only_when_something_is_chosen_and_back_returns_to_more_options(self):
        none = subtitle_menu.subtitles_menu(SubChoice(), many_tracks(3), 0, RID)
        self.assertNotIn("Clear", labels(none))
        some = subtitle_menu.subtitles_menu(SubChoice(["l0"]), many_tracks(3), 0, RID)
        self.assertIn("Clear", labels(some))
        self.assertIn(f"dl|moreq|{RID}", datas(some))

    def test_callbacks_fit_the_64_byte_limit(self):
        for menu in (subtitle_menu.subtitles_menu(SubChoice(["l0"]), many_tracks(200), 24, RID),):
            for data in datas(menu):
                self.assertLessEqual(len(data.encode()), 64, data)

    def test_text_names_the_selection_pages_and_the_player_caveat_and_escapes(self):
        tracks = [SubTrack("fa", "Persian"), SubTrack("x", "<b>Odd</b>")] + many_tracks(20)
        text = subtitle_menu.subtitles_text(SubChoice(["fa", "x"]), tracks, 0, "<i>T</i>", "careful <u>")
        self.assertIn("Selected: Persian, &lt;b&gt;Odd&lt;/b&gt; · embedded track", text)
        self.assertIn("Page 1 of 3", text)
        self.assertIn("&lt;i&gt;T&lt;/i&gt;", text)
        self.assertIn("⚠ careful &lt;u&gt;", text)
        self.assertIn("may not show an embedded track", text)
        self.assertIn("Selected: none", subtitle_menu.subtitles_text(SubChoice(), tracks, 0))


# ============================================================ yt-dlp options
def options(**overrides):
    settings = dict(DEFAULTS, mode="video", quality="best", **overrides)
    return ytdlp_handler._build_opts("https://youtu.be/x", Path("/tmp/ws"), settings, 1, lambda d: None, lambda d: None)


def keys(opts):
    return [pp["key"] for pp in opts["postprocessors"]]


class Options(unittest.TestCase):
    def test_embed_mode(self):
        opts = options(sub_langs=["fa", "en"], sub_mode="embed")
        self.assertTrue(opts["writesubtitles"] and opts["writeautomaticsub"])         # human-made if there is one, else automatic
        self.assertEqual(opts["subtitleslangs"], ["fa", "en"])
        self.assertEqual(keys(opts)[:2], ["FFmpegSubtitlesConvertor", "FFmpegEmbedSubtitle"])
        self.assertEqual(opts["postprocessors"][0]["format"], "srt")
        self.assertFalse(opts["postprocessors"][1]["already_have_subtitle"])          # the .srt files are removed after embedding

    def test_both_keeps_the_files(self):
        opts = options(sub_langs=["fa"], sub_mode="both")
        self.assertTrue(next(pp for pp in opts["postprocessors"] if pp["key"] == "FFmpegEmbedSubtitle")["already_have_subtitle"])

    def test_file_mode_never_embeds(self):
        opts = options(sub_langs=["fa"], sub_mode="file")
        self.assertNotIn("FFmpegEmbedSubtitle", keys(opts))
        self.assertIn("FFmpegSubtitlesConvertor", keys(opts))

    def test_subtitle_steps_run_before_thumbnail_and_metadata(self):
        opts = options(sub_langs=["fa"], sub_mode="embed")
        order = keys(opts)
        self.assertLess(order.index("FFmpegEmbedSubtitle"), order.index("EmbedThumbnail"))
        self.assertLess(order.index("FFmpegEmbedSubtitle"), order.index("FFmpegMetadata"))

    def test_nothing_changes_without_a_choice_and_audio_ignores_it(self):
        plain = options()
        self.assertNotIn("writesubtitles", plain)
        self.assertNotIn("FFmpegEmbedSubtitle", keys(plain))
        audio = ytdlp_handler._build_opts("https://youtu.be/x", Path("/tmp/ws"),
                                          dict(DEFAULTS, mode="audio", sub_langs=["fa"]), 1, lambda d: None, lambda d: None)
        self.assertNotIn("writesubtitles", audio)


# ============================================================ the safety net
class SafetyNet(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="subs-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        yt_dlp.reset([])

    async def run_download(self, **settings):
        full = dict(DEFAULTS, mode="video", quality="best", **settings)
        files = await ytdlp_handler.download("https://youtu.be/x", self.workspace, full, 1, lambda *a: None)
        return files, full

    async def test_a_failure_with_subtitles_retries_without_them_and_says_so(self):
        yt_dlp.reset(["fail"])
        files, settings = await self.run_download(sub_langs=["fa"], sub_mode="embed")
        self.assertEqual([f.name for f in files], ["video.mp4"])
        self.assertEqual(len(yt_dlp.CALLS), 2)
        self.assertIn("writesubtitles", yt_dlp.CALLS[0])
        self.assertNotIn("writesubtitles", yt_dlp.CALLS[1])                       # the retry has no subtitle options
        self.assertEqual(len(settings["delivery_notes"]), 1)
        self.assertIn("without them", settings["delivery_notes"][0])

    async def test_leftovers_of_the_failed_attempt_are_cleared(self):
        (self.workspace / "half.f251.webm.part").write_bytes(b"x")
        (self.workspace / "dir").mkdir()
        yt_dlp.reset(["fail"])
        files, _ = await self.run_download(sub_langs=["fa"])
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), ["video.mp4"])

    async def test_without_subtitles_a_failure_is_not_retried(self):
        yt_dlp.reset(["fail"])
        with self.assertRaises(Exception):
            await self.run_download()
        self.assertEqual(len(yt_dlp.CALLS), 1)

    async def test_a_failure_that_also_happens_without_subtitles_still_surfaces(self):
        yt_dlp.reset(["fail", "fail"])
        with self.assertRaises(Exception):
            await self.run_download(sub_langs=["fa"])

    async def test_a_bot_check_is_handled_by_the_existing_retry_not_blamed_on_subtitles(self):
        yt_dlp.reset(["botcheck", "ok"])
        files, settings = await self.run_download(sub_langs=["fa"])
        self.assertEqual(len(files), 1)
        self.assertEqual(len(yt_dlp.CALLS), 2)
        self.assertIn("writesubtitles", yt_dlp.CALLS[1])                          # the retry kept the subtitle request
        notes = settings.get("delivery_notes", [])
        self.assertFalse([n for n in notes if "couldn't be added" in n])          # not the "retried without subtitles" note
        self.assertTrue([n for n in notes if "No subtitles could be fetched" in n])   # the placeholder video has none: the check says so

    async def test_a_download_whose_video_has_no_subtitle_track_tells_the_person(self):
        files, settings = await self.run_download(sub_langs=["fa"], sub_mode="embed")      # the placeholder video has none
        self.assertEqual(len(settings["delivery_notes"]), 1)
        self.assertIn("No subtitles could be fetched", settings["delivery_notes"][0])

    async def test_a_download_without_a_subtitle_request_never_mentions_subtitles(self):
        files, settings = await self.run_download()
        self.assertNotIn("delivery_notes", settings)

    async def test_clips_never_carry_subtitles(self):
        yt_dlp.reset([])
        full = dict(DEFAULTS, mode="video", quality="best", sub_langs=["fa"], sections=[(0.0, 3.0)])
        await ytdlp_handler.download("https://youtu.be/x", self.workspace, full, 1, lambda *a: None)
        self.assertNotIn("writesubtitles", yt_dlp.CALLS[0])
        self.assertNotIn("delivery_notes", full)


# ============================================================ did they actually arrive?
def make_video(path: Path, subtitle_langs: int) -> Path:
    base = [ "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=2:r=5"]
    srt = path.with_suffix(".srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    inputs, maps = [], ["-map", "0:v"]
    for i in range(subtitle_langs):
        inputs += ["-i", str(srt)]
        maps += ["-map", f"{i + 1}:0"]
    subprocess.run([*base, *inputs, *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:s", "mov_text", str(path)], check=True)
    srt.unlink()
    return path


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class Verification(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="subs-v-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def srt(self, name):
        path = self.tmp / name
        path.write_text("1\n")
        return path

    async def check(self, files, **settings):
        full = {"sub_langs": ["fa", "en"], "sub_mode": "embed", **settings}
        await ytdlp_handler._verify_subtitles(files, full)
        return full.get("delivery_notes", [])

    async def test_no_embedded_track_at_all(self):
        notes = await self.check([make_video(self.tmp / "v.mp4", 0)])
        self.assertEqual(len(notes), 1)
        self.assertIn("No subtitles could be fetched", notes[0])

    async def test_only_some_languages_arrived(self):
        notes = await self.check([make_video(self.tmp / "v.mp4", 1)])
        self.assertEqual(notes, ["◧ Only 1 of 2 subtitle languages could be fetched."])

    async def test_all_languages_arrived_is_silent(self):
        self.assertEqual(await self.check([make_video(self.tmp / "v.mp4", 2)]), [])

    async def test_file_mode_counts_the_srt_files(self):
        video = make_video(self.tmp / "v.mp4", 0)
        self.assertIn("No subtitles", (await self.check([video], sub_mode="file"))[0])
        both = await self.check([video, self.srt("v.fa.srt"), self.srt("v.en.srt")], sub_mode="file")
        self.assertEqual(both, [])
        one = await self.check([video, self.srt("v.fa.srt")], sub_mode="file")
        self.assertEqual(one, ["◧ Only 1 of 2 subtitle languages could be fetched."])

    async def test_both_mode_is_satisfied_by_either(self):
        video = make_video(self.tmp / "v.mp4", 0)
        self.assertEqual(await self.check([video, self.srt("a.srt"), self.srt("b.srt")], sub_mode="both"), [])

    async def test_if_ffprobe_cannot_say_nothing_is_claimed(self):
        original = ytdlp_handler._subtitle_streams
        ytdlp_handler._subtitle_streams = lambda path: asyncio.sleep(0, result=-1)
        self.addCleanup(setattr, ytdlp_handler, "_subtitle_streams", original)
        self.assertEqual(await self.check([make_video(self.tmp / "v.mp4", 0)]), [])


# ============================================================ delivery
class FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []
        self.deleted: list[int] = []

    async def send_video(self, chat_id, file, **kw):
        self.calls.append(("video", file.filename, kw.get("reply_markup"), kw.get("caption", "")))

    async def send_document(self, chat_id, file, **kw):
        self.calls.append(("document", file.filename, kw.get("reply_markup"), kw.get("caption", "")))

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("message", text, None, ""))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_message_text(self, *a, **k):
        pass

    edit_message_caption = edit_message_text


class Delivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="subs-d-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bot = FakeBot()
        self.manager = jm.JobManager(self.bot, max_concurrent=1)

    def files(self, *names):
        paths = []
        for name in names:
            path = self.tmp / name
            path.write_bytes(b"x" * 50)
            paths.append(path)
        return paths

    def job(self, **settings):
        return jm.Job(rid=RID, user_id=1, chat_id=7, url="https://youtu.be/x", settings=settings,
                      status_message_id=55, is_photo=False, header="Sending")

    async def test_the_video_goes_first_then_each_subtitle_file_as_a_document(self):
        await self.manager._send_files(self.job(mode="video"),
                                       self.files("Title [id].fa.srt", "Title [id].mp4", "Title [id].en.srt"))
        self.assertEqual([(kind, name) for kind, name, *_ in self.bot.calls],
                         [("video", "Title [id].mp4"), ("document", "Title [id].fa.srt"), ("document", "Title [id].en.srt")])
        self.assertEqual([c[3] for c in self.bot.calls[1:]], ["◧ Subtitles · fa", "◧ Subtitles · en"])
        self.assertIsNotNone(self.bot.calls[0][2])                                 # the video keeps its send-as-file button
        self.assertEqual(self.bot.deleted, [55])

    async def test_subtitle_files_do_not_turn_one_video_into_several(self):
        await self.manager._send_files(self.job(mode="video"), self.files("v.mp4", "v.fa.srt"))
        video = self.bot.calls[0]
        self.assertIn("▤ Send as file instead", labels(video[2]))                  # single-file wording, not "Send all as files"

    async def test_a_file_without_a_language_is_still_sent(self):
        await self.manager._send_files(self.job(mode="video"), self.files("v.mp4", "weird.name.srt", "plain.srt"))
        captions = [c[3] for c in self.bot.calls[1:]]
        self.assertEqual(captions, ["◧ Subtitles", "◧ Subtitles"])

    async def test_history_title_and_cache_ignore_subtitle_files(self):
        files = self.files("a.fa.srt", "My video.mp4")

        async def fake_download(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            return files

        original = (jm.dispatch_download, jm.job_workspace)
        jm.dispatch_download = fake_download
        jm.job_workspace = lambda: contextlib.nullcontext(self.tmp)
        self.addCleanup(lambda: (setattr(jm, "dispatch_download", original[0]), setattr(jm, "job_workspace", original[1])))
        job = self.job(mode="video")
        await self.manager._run_job(job)
        self.assertEqual(job.title, "My video")
        self.assertEqual([e["name"] for e in self.manager._recent_files[RID]], ["My video.mp4"])

    async def test_delivery_notes_are_sent_once_after_the_files(self):
        files = self.files("v.mp4")

        async def fake_download(url, workspace, settings, user_id, progress_cb, cancel_event=None):
            settings.setdefault("delivery_notes", []).append("◧ No subtitles could be fetched.")
            return files

        original = (jm.dispatch_download, jm.job_workspace)
        jm.dispatch_download = fake_download
        jm.job_workspace = lambda: contextlib.nullcontext(self.tmp)
        self.addCleanup(lambda: (setattr(jm, "dispatch_download", original[0]), setattr(jm, "job_workspace", original[1])))
        job = self.job(mode="video")
        await self.manager._run_job(job)
        kinds = [c[0] for c in self.bot.calls]
        self.assertEqual(kinds, ["video", "message"])
        self.assertEqual(self.bot.calls[1][1], "◧ No subtitles could be fetched.")
        self.assertNotIn("delivery_notes", job.settings)                           # a retry won't repeat it


if __name__ == "__main__":
    unittest.main()
