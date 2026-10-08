"""
The media toolbox: ffmpeg operations on files people send (real ffmpeg on tiny generated files), the typed-input
parsers, the stored uploads, the menus and handlers in main.py, and delivery through the job manager.
"""
import asyncio
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import main  # noqa: E402
from downloader import tools, ytdlp_handler  # noqa: E402
from downloader.errors import JobCancelled  # noqa: E402
from jobqueue import job_manager as jm  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402
from ui import tools_menu  # noqa: E402
from tests.test_sections_flow import FakeBot, FakeContext, FakeQuery, FakeUpdate, FakeMessage, datas, labels  # noqa: E402

_REAL_SUBTITLE_PASS = ytdlp_handler._subtitle_pass      # (the recovery tests replace it on the module)

USER = 42
RID = "tool000001"
SRT = "1\n00:00:01,000 --> 00:00:03,000\nسلام دنیا Hello\n\n"

_TMP = Path(tempfile.mkdtemp(prefix="toolbox-"))
VIDEO = _TMP / "video.mp4"            # 6 s, 320x180, with sound and a title tag
SILENT = _TMP / "silent.mp4"          # video, no audio
SOUND = _TMP / "sound.mp3"            # audio only
SUBS = _TMP / "subs.srt"


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def setUpModule():
    _ffmpeg("-f", "lavfi", "-i", "testsrc=duration=6:size=320x180:rate=25", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", "-metadata", "title=Secret", str(VIDEO))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=duration=4:size=320x180:rate=25", "-c:v", "libx264", str(SILENT))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-c:a", "libmp3lame", str(SOUND))
    SUBS.write_text(SRT, encoding="utf-8")


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def probe_sync(path):
    """tools.probe without an event loop (the tests are already inside one)."""
    import json
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    data = json.loads(out)
    info = tools.MediaInfo(size=Path(path).stat().st_size, duration=float(data["format"]["duration"]))
    for stream in data["streams"]:
        if stream["codec_type"] == "video" and not info.has_video:
            info.has_video, info.width, info.height = True, stream.get("width"), stream.get("height")
        elif stream["codec_type"] == "audio":
            info.has_audio = True
    return info


def ffprobe(path, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "default=nw=1", str(path)],
                         capture_output=True, text=True)
    return out.stdout


# ============================================================ typed input
class Parsing(unittest.TestCase):
    def test_a_range_is_two_times_with_any_separator(self):
        for text in ("1:20 2:45", "1:20-2:45", "1:20 – 2:45", "1:20 to 2:45", "1:20، 2:45"):
            self.assertEqual(tools.parse_range(text, 300), (80.0, 165.0), text)

    def test_only_a_start_means_until_the_end_and_persian_digits_work(self):
        self.assertEqual(tools.parse_range("1:00", 90), (60.0, 90))
        self.assertEqual(tools.parse_range("۱:۰۰ ۲:۰۰", 300), (60.0, 120.0))

    def test_an_end_past_the_file_is_clamped_but_a_start_past_it_is_refused(self):
        self.assertEqual(tools.parse_range("0:10 9:00", 60), (10.0, 60))
        with self.assertRaisesRegex(tools.ToolError, "past the end"):
            tools.parse_range("2:00", 60)

    def test_nonsense_is_refused_with_a_helpful_message(self):
        for text in ("", "a b", "1 2 3", "5:00 1:00", "1:75 2:00"):
            with self.assertRaises(tools.ToolError, msg=text):
                tools.parse_range(text, 600)

    def test_gif_input_has_a_default_length_a_cap_and_stops_at_the_end(self):
        self.assertEqual(tools.parse_gif("1:20", 600), (80.0, 5))
        self.assertEqual(tools.parse_gif("1:20 8", 600), (80.0, 8.0))
        self.assertEqual(tools.parse_gif("0:58", 60), (58.0, 2.0))
        with self.assertRaisesRegex(tools.ToolError, "at most"):
            tools.parse_gif("0:10 99", 600)
        with self.assertRaises(tools.ToolError):
            tools.parse_gif("0:10 0", 600)

    def test_which_documents_are_media(self):
        self.assertTrue(tools.looks_like_media("a.mkv", None))
        self.assertTrue(tools.looks_like_media("noext", "video/x-matroska"))
        self.assertTrue(tools.looks_like_media("a.FLAC", "application/octet-stream"))
        self.assertFalse(tools.looks_like_media("report.pdf", "application/pdf"))
        self.assertFalse(tools.looks_like_media("", None))


class Compress(unittest.TestCase):
    def test_the_bitrate_follows_the_size_and_the_length(self):
        kbps, height = tools.plan_compress(60, 25, 1080)
        self.assertTrue(2500 < kbps < 3300, kbps)                    # ~25 MB over a minute
        self.assertIsNone(height)                                    # plenty of room: keep the resolution

    def test_a_tight_budget_steps_the_resolution_down_but_never_up(self):
        _, height = tools.plan_compress(300, 25, 1080)
        self.assertEqual(height, 480)
        _, height = tools.plan_compress(600, 25, 1080)
        self.assertEqual(height, 360)
        _, height = tools.plan_compress(300, 25, 360)
        self.assertIsNone(height)

    def test_a_hopeless_size_is_refused_before_anything_runs(self):
        with self.assertRaisesRegex(tools.ToolError, "too long"):
            tools.plan_compress(4 * 3600, 10, 1080)
        with self.assertRaisesRegex(tools.ToolError, "how long"):
            tools.plan_compress(None, 10, 1080)


class Subtitles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="srt-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_old_persian_subtitle_encodings_become_utf8(self):
        for encoding in ("cp1256", "utf-16", "utf-8-sig"):
            path = self.dir / f"{encoding}.srt"
            path.write_bytes("1\r\n00:00:01,000 --> 00:00:02,000\r\nسلام خوب\r\n".encode(encoding))
            tools.normalize_srt(path)
            self.assertIn("سلام خوب", path.read_text(encoding="utf-8"), encoding)
            self.assertNotIn("\r", path.read_text(encoding="utf-8"))

    def test_something_that_is_not_subtitles_is_refused(self):
        for content in (b"", b"just some text"):
            path = self.dir / "x.srt"
            path.write_bytes(content)
            with self.assertRaises(tools.ToolError):
                tools.normalize_srt(path)


# ============================================================ stored uploads
class Store(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="store-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.now = 1000.0
        self.store = tools.InputStore(self.root, ttl=100, clock=lambda: self.now)

    def put(self, rid="a", user=1):
        path = self.store.folder(rid) / "original.mp4"
        path.write_bytes(b"x")
        return self.store.add(rid, user, path, "n.mp4", tools.MediaInfo())

    def test_only_the_owner_gets_the_file_back(self):
        self.put()
        self.assertIsNotNone(self.store.get("a", 1))
        self.assertIsNone(self.store.get("a", 2))
        self.assertIsNone(self.store.get("zzz", 1))

    def test_files_expire_and_their_folder_is_deleted(self):
        self.put()
        self.now += 101
        self.assertIsNone(self.store.get("a", 1))
        self.assertFalse((self.root / "a").exists())

    def test_discard_removes_the_file_from_disk(self):
        self.put()
        self.store.discard("a")
        self.assertFalse((self.root / "a").exists())
        self.assertEqual(self.store.for_user(1), [])

    def test_a_file_that_vanished_from_disk_is_treated_as_gone(self):
        item = self.put()
        item.path.unlink()
        self.assertIsNone(self.store.get("a", 1))


# ============================================================ ffmpeg, for real
class Running(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="toolrun-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.events: list = []

    def settings(self, source=VIDEO, **extra):
        info = probe_sync(source)
        return {"tool_input": str(source), "tool_name": source.name, "tool_duration": info.duration,
                "tool_has_video": info.has_video, "tool_height": info.height, **extra}

    async def run_tool(self, source=VIDEO, cancel=None, **extra):
        return await tools.run(self.settings(source, **extra), self.workspace, lambda *a: self.events.append(a), cancel)

    async def test_probe_reads_streams_and_refuses_non_media(self):
        info = await tools.probe(VIDEO)
        self.assertEqual((info.has_video, info.has_audio, info.width, info.height), (True, True, 320, 180))
        self.assertAlmostEqual(info.duration, 6, delta=0.3)
        audio = await tools.probe(SOUND)
        self.assertEqual((audio.has_video, audio.has_audio), (False, True))
        junk = self.workspace / "junk.mp4"
        junk.write_bytes(b"not media at all")
        with self.assertRaises(tools.ToolError):
            await tools.probe(junk)

    async def test_trim_exact_has_the_requested_length(self):
        [out] = await self.run_tool(tool="trim", start=1, end=3.5, exact=True)
        self.assertAlmostEqual(probe_sync(out).duration, 2.5, delta=0.25)
        self.assertEqual(out.suffix, ".mp4")

    async def test_trim_fast_copies_the_streams(self):
        [out] = await self.run_tool(tool="trim", start=1, end=4, exact=False)
        self.assertLess(probe_sync(out).duration, 4.5)
        self.assertIn("h264", ffprobe(out, "stream=codec_name"))

    async def test_trimming_audio_keeps_its_format(self):
        [out] = await self.run_tool(SOUND, tool="trim", start=1, end=3, exact=True)
        self.assertEqual(out.suffix, ".mp3")
        self.assertAlmostEqual(probe_sync(out).duration, 2, delta=0.3)

    async def test_extract_audio_in_both_formats_has_no_picture(self):
        for fmt in tools.AUDIO_FORMATS:
            [out] = await self.run_tool(tool="audio", audio_format=fmt)
            self.assertEqual(out.suffix, f".{fmt}")
            info = probe_sync(out)
            self.assertEqual((info.has_video, info.has_audio), (False, True))

    async def test_extracting_audio_from_a_silent_video_says_what_is_wrong(self):
        with self.assertRaises(tools.ToolError):
            await self.run_tool(SILENT, tool="audio", audio_format="mp3")

    async def test_compress_lands_under_the_target(self):
        [out] = await self.run_tool(tool="compress", target_mb=0.4)
        self.assertLess(out.stat().st_size, 0.4 * 1_000_000)
        self.assertTrue(probe_sync(out).has_audio)

    async def test_gif_is_an_animated_gif_of_the_right_length(self):
        [out] = await self.run_tool(tool="gif", start=1, length=2)
        self.assertEqual(out.read_bytes()[:6], b"GIF89a")
        self.assertAlmostEqual(probe_sync(out).duration, 2, delta=0.4)

    async def test_strip_removes_metadata_and_keeps_the_picture_and_sound(self):
        self.assertIn("Secret", ffprobe(VIDEO, "format_tags=title"))
        [out] = await self.run_tool(tool="strip")
        self.assertNotIn("Secret", ffprobe(out, "format_tags=title"))
        info = probe_sync(out)
        self.assertEqual((info.has_video, info.has_audio), (True, True))
        self.assertAlmostEqual(info.duration, 6, delta=0.3)

    async def test_burn_draws_the_subtitle_into_the_picture(self):
        [out] = await self.run_tool(tool="burn", tool_srt=str(SUBS))
        self.assertEqual(out.name, "video [subtitled].mp4")
        self.assertEqual(ffprobe(out, "stream=codec_type").count("subtitle"), 0)     # drawn, not a track
        frame = lambda path, at: subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True).stdout
        self.assertNotEqual(frame(out, 2), frame(VIDEO, 2))                          # text is on the frame at 2 s

    async def test_progress_is_reported_and_ends_at_100(self):
        await self.run_tool(tool="compress", target_mb=0.3)
        self.assertEqual(self.events[-1], (100.0, None, None, None, "Compressing"))
        # Every tick is reported as a LABEL (5th argument), never as a stage: a stage starts a new line each time.
        self.assertTrue(all(event[3] is None and event[4] == "Compressing" for event in self.events))

    async def test_cancelling_stops_ffmpeg_and_raises(self):
        event = asyncio.Event()
        event.set()
        with self.assertRaises(JobCancelled):
            await self.run_tool(cancel=event, tool="compress", target_mb=0.3)

    async def test_a_timeout_stops_ffmpeg(self):
        self.addCleanup(setattr, tools, "TOOL_TIMEOUT_SECONDS", tools.TOOL_TIMEOUT_SECONDS)
        tools.TOOL_TIMEOUT_SECONDS = 0
        with self.assertRaisesRegex(tools.ToolError, "too long"):
            await self.run_tool(tool="compress", target_mb=0.3)

    async def test_a_missing_input_or_subtitle_file_is_explained(self):
        with self.assertRaisesRegex(tools.ToolError, "send it again"):
            await tools.run({"tool": "strip", "tool_input": str(self.workspace / "gone.mp4")}, self.workspace, lambda *a: None, None)
        with self.assertRaisesRegex(tools.ToolError, "subtitle file"):
            await self.run_tool(tool="burn", tool_srt=str(self.workspace / "gone.srt"))

    async def test_unknown_tools_and_unsafe_names(self):
        with self.assertRaises(tools.ToolError):
            await self.run_tool(tool="explode")
        [out] = await tools.run({**self.settings(tool="strip"), "tool_name": "../../etc/pass:wd?.mp4"}, self.workspace,
                                lambda *a: None, None)
        self.assertEqual(out.parent, self.workspace)
        self.assertNotIn("/", out.name)


# ============================================================ downloads with burned-in subtitles
class BurnedInDownloads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="burn-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.video = self.workspace / "Clip.mp4"
        shutil.copy(VIDEO, self.video)
        self.fa = self.workspace / "Clip.fa.srt"
        self.en = self.workspace / "Clip.en.srt"
        self.fa.write_text(SRT, encoding="utf-8")
        self.en.write_text(SRT.replace("Hello", "Hi"), encoding="utf-8")

    async def burn(self, files, **settings):
        full = {"sub_langs": ["fa", "en"], "sub_mode": "burn", "delivery_notes": [], **settings}
        result = await ytdlp_handler._burn_subtitles(files, self.workspace, full, lambda *a: None, None)
        return result, full

    async def test_the_first_language_is_burned_in_and_the_rest_stay_as_srt_files(self):
        result, settings = await self.burn([self.video, self.fa, self.en])
        names = sorted(f.name for f in result)
        self.assertEqual(names, ["Clip.en.srt", "Clip.mp4"])
        self.assertFalse(self.fa.exists())                                          # burned: no need to send it too
        self.assertTrue(any("first language" in note for note in settings["delivery_notes"]))
        self.assertTrue(probe_sync(self.workspace / "Clip.mp4").has_video)

    async def test_the_order_the_person_picked_decides_which_language_is_burned(self):
        result, _ = await self.burn([self.video, self.fa, self.en], sub_langs=["en", "fa"])
        self.assertEqual(sorted(f.name for f in result), ["Clip.fa.srt", "Clip.mp4"])

    async def test_a_single_language_needs_no_note(self):
        result, settings = await self.burn([self.video, self.fa])
        self.assertEqual([f.name for f in result], ["Clip.mp4"])
        self.assertEqual(settings["delivery_notes"], [])

    async def test_a_video_that_is_too_long_goes_out_unchanged_with_its_srt(self):
        self.addCleanup(setattr, ytdlp_handler.config, "BURN_MAX_SECONDS", ytdlp_handler.config.BURN_MAX_SECONDS)
        ytdlp_handler.config.BURN_MAX_SECONDS = 2
        files = [self.video, self.fa]
        result, settings = await self.burn(files)
        self.assertEqual(result, files)
        self.assertTrue(any("too long" in note for note in settings["delivery_notes"]))

    async def test_if_ffmpeg_cannot_burn_the_download_is_not_lost(self):
        async def broken(*a, **k):
            raise tools.ToolError("boom")
        self.addCleanup(setattr, tools, "burn_into", tools.burn_into)
        tools.burn_into = broken
        files = [self.video, self.fa]
        result, settings = await self.burn(files)
        self.assertEqual(result, files)
        self.assertTrue(self.video.exists() and self.fa.exists())
        self.assertTrue(any("Couldn't burn" in note for note in settings["delivery_notes"]))

    async def test_cancelling_while_burning_is_not_swallowed(self):
        async def cancelled(*a, **k):
            raise JobCancelled("x")
        self.addCleanup(setattr, tools, "burn_into", tools.burn_into)
        tools.burn_into = cancelled
        with self.assertRaises(JobCancelled):
            await self.burn([self.video, self.fa])

    async def test_a_download_in_burn_mode_comes_out_burned(self):
        async def fake_main(url, workspace, settings, user_id, cb, cancel_event=None):
            return [self.video, self.fa]
        self.addCleanup(setattr, ytdlp_handler, "_download_main", ytdlp_handler._download_main)
        ytdlp_handler._download_main = fake_main
        settings = {"sub_langs": ["fa"], "sub_mode": "burn", "delivery_notes": [], "mode": "video"}
        files = await ytdlp_handler._download_with_subtitles("https://youtu.be/x", self.workspace, settings, 1, lambda *a: None)
        self.assertEqual([f.name for f in files], ["Clip.mp4"])
        self.assertFalse(self.fa.exists())

    async def test_other_modes_never_burn(self):
        async def fake_main(url, workspace, settings, user_id, cb, cancel_event=None):
            return [self.video, self.fa]
        self.addCleanup(setattr, ytdlp_handler, "_download_main", ytdlp_handler._download_main)
        ytdlp_handler._download_main = fake_main
        settings = {"sub_langs": ["fa"], "sub_mode": "file", "delivery_notes": [], "mode": "video"}
        files = await ytdlp_handler._download_with_subtitles("https://youtu.be/x", self.workspace, settings, 1, lambda *a: None)
        self.assertEqual(sorted(f.name for f in files), ["Clip.fa.srt", "Clip.mp4"])
        self.assertTrue(self.fa.exists())

    async def test_nothing_to_burn_changes_nothing(self):
        result, _ = await self.burn([self.video])
        self.assertEqual(result, [self.video])

    async def test_the_verification_counts_srt_files_in_burn_mode(self):
        settings = {"sub_langs": ["fa", "en"], "sub_mode": "burn", "delivery_notes": []}
        await ytdlp_handler._verify_subtitles([self.video, self.fa], settings)
        self.assertTrue(any("Only 1 of 2" in note for note in settings["delivery_notes"]))
        settings["delivery_notes"].clear()
        await ytdlp_handler._verify_subtitles([self.video], settings)
        self.assertTrue(any("No subtitles" in note for note in settings["delivery_notes"]))


# ============================================================ Persian burned-in subtitles
PERSIAN_SRT = ("1\n00:00:00,500 --> 00:00:03,000\nسلام دنیا، این یک آزمایش است\nپ چ ژ گ ک ی — Hello\n\n")
EMPTY_FONTCONFIG = '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig></fontconfig>'


class PersianBurnIn(unittest.IsolatedAsyncioTestCase):
    """Burned-in Persian showed empty boxes: the container had no font libass could use. The font now ships with
    the bot, and these tests run ffmpeg with NO system fonts at all (an empty fontconfig) to prove it."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fa-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "empty.conf").write_text(EMPTY_FONTCONFIG)
        self.srt = self.dir / "fa.srt"
        self.srt.write_text(PERSIAN_SRT, encoding="utf-8")
        import os
        from unittest import mock
        patcher = mock.patch.dict(os.environ, {"FONTCONFIG_FILE": str(self.dir / "empty.conf")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def flat_video(self):
        """A plain one-colour video: after burning, only subtitle ink can differ from the background."""
        path = self.dir / "flat.mp4"
        _ffmpeg("-f", "lavfi", "-i", "color=c=0x335577:s=320x180:r=25:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
        return path

    def ink(self, path, at):
        """How many pixels of the frame at `at` seconds differ clearly from the flat background."""
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        background = max(set(raw), key=raw.count)
        return sum(1 for pixel in raw if abs(pixel - background) > 40)

    def libass_log(self, args):
        """What libass says about fonts while burning (ffmpeg -v verbose)."""
        return subprocess.run(["ffmpeg", "-v", "verbose", "-y", "-nostdin", *args], capture_output=True, text=True,
                              cwd=self.dir).stderr

    def test_the_font_ships_with_the_bot(self):
        for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "LICENSE-DejaVu.txt"):
            self.assertTrue((tools.FONTS_DIR / name).is_file(), name)
        self.assertGreater((tools.FONTS_DIR / "DejaVuSans.ttf").stat().st_size, 100_000)

    def test_the_filter_points_at_the_bundled_fonts_and_names_the_font(self):
        args = tools.burn_args(Path("in.mp4"), "burn_subs.srt", Path("out.mp4"))
        vf = args[args.index("-vf") + 1]
        self.assertIn(f"fontsdir={tools.FONTS_DIR}", vf)
        self.assertIn("FontName=DejaVu Sans", vf)
        self.assertTrue(vf.startswith("subtitles=burn_subs.srt:"))

    def test_the_font_directory_is_escaped_for_the_filter(self):
        self.assertEqual(tools._filter_escape("/a b/c:d,e'f"), "/a b/c\\:d\\,e\\'f")

    def test_without_the_bundled_font_a_bare_container_draws_nothing(self):
        """The bug, reproduced: this is what the old setup did on an image without a usable font."""
        shutil.copy(VIDEO, self.dir / "in.mp4")
        shutil.copy(self.srt, self.dir / "burn_subs.srt")
        log = self.libass_log(["-i", "in.mp4", "-vf", "subtitles=burn_subs.srt:force_style='FontName=Noto Sans,Fontsize=22'",
                               "-t", "3", "-f", "null", "-"])
        self.assertIn("failed to find any fallback", log)

    def test_persian_is_found_in_the_bundled_font_with_no_fallback_needed(self):
        shutil.copy(VIDEO, self.dir / "in.mp4")
        shutil.copy(self.srt, self.dir / "burn_subs.srt")
        args = tools.burn_args(Path("in.mp4"), "burn_subs.srt", self.dir / "out.mp4")
        log = self.libass_log([*args[:args.index("-c:v")], "-t", "3", "-f", "null", "-"])
        self.assertIn("-> DejaVuSans", log)
        self.assertNotIn("failed to find any fallback", log)          # no glyph (= no empty box) was left unfound

    async def test_the_burned_video_really_shows_persian_text_with_no_system_fonts(self):
        flat = self.flat_video()
        out = await tools.burn_into(flat, self.srt, self.dir, 6, lambda *a: None, None, stem="fa")
        self.assertGreater(self.ink(out, 2), 300, "no subtitle ink on the frame")        # the text is drawn ...
        self.assertLess(self.ink(out, 5), 20)                                            # ... and gone after its end time
        self.assertEqual(self.ink(flat, 2), 0)                                           # (the source itself is flat)

    async def test_the_same_video_without_the_bundled_font_stays_blank_which_is_the_bug(self):
        flat = self.flat_video()
        original_dir = tools.FONTS_DIR
        self.addCleanup(setattr, tools, "FONTS_DIR", original_dir)
        tools.FONTS_DIR = self.dir / "no-such-folder"
        out = await tools.burn_into(flat, self.srt, self.dir, 6, lambda *a: None, None, stem="bare")
        self.assertLess(self.ink(out, 2), 20)


# ============================================================ subtitles that YouTube refused the first time
class SubtitleRecovery(unittest.IsolatedAsyncioTestCase):
    """HTTP 429 on the subtitle request: the video still arrives, and the subtitles get their own second chance."""

    ERROR_429 = "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="recover-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.passes: list[list[str]] = []
        self.arrives: list[set] = []            # per attempt: which languages the "server" finally hands over
        self.mains: list[dict] = []
        self.main_error: Exception | None = RuntimeError(self.ERROR_429)

        async def fake_main(url, workspace, settings, user_id, cb, cancel_event=None):
            self.mains.append(dict(settings))
            if settings.get("sub_langs") and self.main_error:
                raise self.main_error
            target = workspace / "Clip.mp4"
            shutil.copy(VIDEO, target)
            return [target]

        async def fake_pass(url, workspace, settings, user_id, langs):
            attempt = len(self.passes)
            self.passes.append(list(langs))
            for lang in (self.arrives[attempt] if attempt < len(self.arrives) else set()):
                if lang in langs:
                    (workspace / f"Clip.{lang}.srt").write_text(SRT.replace("Hello", lang), encoding="utf-8")

        for name, value in (("_download_main", fake_main), ("_subtitle_pass", fake_pass),
                            ("SUBTITLE_RETRY_DELAYS", (0, 0, 0))):
            self.addCleanup(setattr, ytdlp_handler, name, getattr(ytdlp_handler, name))
            setattr(ytdlp_handler, name, value)

    async def download(self, langs, mode, **extra):
        settings = {**DEFAULTS, "mode": "video", "quality": "best", "sub_langs": langs, "sub_mode": mode,
                    "delivery_notes": [], **extra}
        files = await ytdlp_handler._download_with_subtitles("https://youtu.be/x", self.workspace, settings, 1, lambda *a: None)
        return files, settings

    def names(self, files):
        return sorted(f.name for f in files)

    def subtitle_tracks(self, path):
        return ffprobe(path, "stream=codec_type").count("subtitle")

    async def test_the_video_is_fetched_without_subtitles_first_then_they_are_retried_on_their_own(self):
        self.arrives = [set(), {"en"}]
        files, settings = await self.download(["en"], "file")
        self.assertEqual(self.names(files), ["Clip.en.srt", "Clip.mp4"])
        self.assertNotIn("sub_langs", self.mains[1])                             # the video itself never carries them
        self.assertEqual(self.passes, [["en"], ["en"]])
        self.assertEqual(settings["delivery_notes"], [])                         # it worked: nothing to apologise for

    async def test_only_the_missing_languages_are_asked_for_again(self):
        self.arrives = [{"fa"}, {"en"}]
        files, settings = await self.download(["fa", "en"], "file")
        self.assertEqual(self.passes, [["fa", "en"], ["en"]])
        self.assertEqual(self.names(files), ["Clip.en.srt", "Clip.fa.srt", "Clip.mp4"])
        self.assertEqual(settings["delivery_notes"], [])

    async def test_embedded_mode_puts_the_tracks_in_the_video_and_leaves_no_srt(self):
        self.arrives = [{"fa", "en"}]
        files, _ = await self.download(["fa", "en"], "embed")
        self.assertEqual(self.names(files), ["Clip.mp4"])
        self.assertEqual(self.subtitle_tracks(files[0]), 2)
        self.assertTrue(probe_sync(files[0]).has_video)

    async def test_embedded_plus_file_gives_both(self):
        self.arrives = [{"fa"}]
        files, _ = await self.download(["fa"], "both")
        self.assertEqual(self.names(files), ["Clip.fa.srt", "Clip.mp4"])
        mp4 = next(f for f in files if f.suffix == ".mp4")
        self.assertEqual(self.subtitle_tracks(mp4), 1)

    async def test_if_embedding_fails_the_subtitles_come_as_files_and_the_person_is_told(self):
        async def broken(*a, **k):
            raise tools.ToolError("ffmpeg said no")
        self.addCleanup(setattr, tools, "embed_subtitles", tools.embed_subtitles)
        tools.embed_subtitles = broken
        self.arrives = [{"fa"}]
        files, settings = await self.download(["fa"], "embed")
        self.assertEqual(self.names(files), ["Clip.fa.srt", "Clip.mp4"])
        self.assertTrue(any("Couldn't embed" in note for note in settings["delivery_notes"]))

    async def test_burning_and_embedding_in_downloads_report_one_updating_line_too(self):
        self.arrives = [{"fa"}]
        seen = []
        settings = {**DEFAULTS, "mode": "video", "quality": "best", "sub_langs": ["fa"], "sub_mode": "burn", "delivery_notes": []}
        await ytdlp_handler._download_with_subtitles("https://youtu.be/x", self.workspace, settings, 1,
                                                     lambda *a: seen.append(a))
        ticks = [a for a in seen if a[0] is not None]
        self.assertTrue(ticks)
        self.assertTrue(all(a[3] is None and a[4] == "Burning subtitles" for a in ticks), ticks)

    async def test_burned_in_draws_the_first_language_and_keeps_the_others_as_files(self):
        self.arrives = [{"fa", "en"}]
        files, settings = await self.download(["en", "fa"], "burn")
        self.assertEqual(self.names(files), ["Clip.fa.srt", "Clip.mp4"])
        mp4 = next(f for f in files if f.suffix == ".mp4")
        self.assertEqual(self.subtitle_tracks(mp4), 0)                           # drawn into the picture, not a track
        self.assertTrue(any("first language" in note for note in settings["delivery_notes"]))

    async def test_burned_in_plus_file_keeps_every_language_as_a_file(self):
        self.arrives = [{"fa", "en"}]
        files, _ = await self.download(["en", "fa"], "burnfile")
        self.assertEqual(self.names(files), ["Clip.en.srt", "Clip.fa.srt", "Clip.mp4"])

    async def test_when_they_never_arrive_the_person_is_told_why_and_the_video_is_not_lost(self):
        files, settings = await self.download(["en"], "embed")
        self.assertEqual(self.names(files), ["Clip.mp4"])
        self.assertEqual(self.passes, [["en"]] * 3)                              # three tries, then it gives up
        [note] = settings["delivery_notes"]
        self.assertIn("HTTP 429", note)
        self.assertIn("Try again in a few minutes", note)

    async def test_a_different_failure_gets_a_plain_note_not_a_429_claim(self):
        self.main_error = RuntimeError("Postprocessing: ffmpeg could not embed")
        _, settings = await self.download(["en"], "embed")
        [note] = settings["delivery_notes"]
        self.assertNotIn("429", note)
        self.assertIn("couldn't be fetched", note)

    async def test_a_partial_result_says_how_many_arrived(self):
        self.arrives = [{"fa"}]
        files, settings = await self.download(["fa", "en"], "file")
        self.assertEqual(self.names(files), ["Clip.fa.srt", "Clip.mp4"])
        [note] = settings["delivery_notes"]
        self.assertIn("Only 1 of 2", note)
        self.assertIn("429", note)

    async def test_a_bot_check_is_still_not_blamed_on_subtitles(self):
        self.main_error = RuntimeError("Sign in to confirm you're not a bot")
        with self.assertRaises(RuntimeError):
            await self.download(["en"], "embed")
        self.assertEqual(self.passes, [])

    async def test_cancelling_during_the_wait_between_tries_is_noticed(self):
        ytdlp_handler.SUBTITLE_RETRY_DELAYS = (0, 30)
        event = asyncio.Event()
        settings = {**DEFAULTS, "mode": "video", "sub_langs": ["en"], "sub_mode": "embed", "delivery_notes": []}
        task = asyncio.create_task(ytdlp_handler._download_with_subtitles(
            "https://youtu.be/x", self.workspace, settings, 1, lambda *a: None, event))
        await asyncio.sleep(0.3)
        event.set()
        with self.assertRaises(JobCancelled):
            await asyncio.wait_for(task, timeout=5)

    async def test_the_subtitle_pass_fetches_only_subtitles_and_ignores_the_archive(self):
        """With "use_archive" on, the just-downloaded video is in the archive: the pass would skip it, subtitles included."""
        captured = {}

        class FakeYDL:
            def __init__(self, opts):
                captured.update(opts)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def download(self, urls):
                captured["urls"] = urls
                return 0

        self.addCleanup(setattr, ytdlp_handler.yt_dlp, "YoutubeDL", getattr(ytdlp_handler.yt_dlp, "YoutubeDL", None))
        ytdlp_handler.yt_dlp.YoutubeDL = FakeYDL
        settings = {**DEFAULTS, "use_archive": True, "proxy": "socks5h://warp:1080", "sub_mode": "embed"}
        await _REAL_SUBTITLE_PASS("https://youtu.be/x", self.workspace, settings, 1, ["fa", "en"])
        self.assertNotIn("download_archive", captured)
        self.assertTrue(captured["skip_download"] and captured["ignoreerrors"])
        self.assertEqual((captured["subtitleslangs"], captured["writesubtitles"], captured["writeautomaticsub"]),
                         (["fa", "en"], True, True))
        self.assertEqual([pp["key"] for pp in captured["postprocessors"]], ["FFmpegSubtitlesConvertor"])   # no embedding here
        self.assertEqual(captured["proxy"], "socks5h://warp:1080")                # the same route as the video
        self.assertEqual(captured["urls"], ["https://youtu.be/x"])


# ============================================================ handlers in main.py
class Media:
    """A fake Telegram video/audio/document that 'downloads' a sample file."""
    def __init__(self, source=VIDEO, name="holiday.mp4", size=None, fail=False):
        self.source, self.file_name, self.mime_type, self.title = source, name, "video/mp4", None
        self.file_path = None                       # where the local Bot API server keeps its copy
        self.file_size = size if size is not None else source.stat().st_size
        self.fail = fail

    async def get_file(self):
        if self.fail:
            raise RuntimeError("telegram said no")
        return self

    async def download_to_drive(self, custom_path=None, **kw):
        shutil.copy(self.source, custom_path)


class Incoming(FakeMessage):
    def __init__(self, **kinds):
        super().__init__()
        for kind in ("video", "animation", "video_note", "audio", "voice", "document", "photo"):
            setattr(self, kind, kinds.get(kind))
        self.replies: list = []

    async def reply_text(self, text, **kw):
        status = Status(self, text, kw.get("reply_markup"))
        self.replies.append(status)
        return status


class Status:
    def __init__(self, parent, text, markup):
        self.parent, self.text, self.markup, self.edits = parent, text, markup, []

    async def edit_text(self, text, **kw):
        self.text, self.markup = text, kw.get("reply_markup")
        self.edits.append(text)


class FakeJobs:
    def __init__(self):
        self.enqueued: list[dict] = []
        self.active: set = set()

    async def enqueue(self, rid, user_id, chat_id, url, settings, status_message_id, **kw):
        self.enqueued.append({"rid": rid, "user": user_id, "url": url, "settings": settings, **kw})

    def is_active(self, rid):
        return rid in self.active

    def cancel(self, rid, user_id):
        return False


class Handlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="uploads-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(setattr, tools, "store", tools.store)
        tools.store = tools.InputStore(self.root)
        self.bot, self.jobs = FakeBot(), FakeJobs()
        for name in ("pending_tool_input", "tool_drafts", "last_download", "pending_section_input", "pending_links",
                     "pending_probes", "pending_sections", "pending_subs"):
            setattr(main, name, {})
        main.job_manager = self.jobs
        main.get_settings = lambda uid: dict(DEFAULTS)

        async def allow(*a, **k):
            return True
        main.gate, main.gate_callback = allow, allow

        async def noop(message):
            pass
        main._delete_quietly = noop

        async def edit_message_text(text, **kw):
            self.bot.record("text", text, kw.get("reply_markup"))
        self.bot.edit_message_text = lambda text, **kw: edit_message_text(text, **kw)

    # -- helpers
    async def send(self, **kinds):
        message = Incoming(**kinds)
        update = FakeUpdate(self.bot)
        update.message = message
        await main.media_handler(update, FakeContext(self.bot))
        return message.replies[-1] if message.replies else None

    async def upload(self, source=VIDEO, name="holiday.mp4"):
        status = await self.send(video=Media(source, name))
        rid = next(iter(tools.store._items))
        return status, rid

    async def tap(self, data):
        await main.tools_callback(FakeUpdate(self.bot, FakeQuery(data, self.bot)), FakeContext(self.bot))
        return self.bot.edits[-1] if self.bot.edits else None

    async def type_text(self, text):
        await main.link_handler(FakeUpdate(self.bot, text=text), FakeContext(self.bot))
        return self.bot.edits[-1]

    # -- receiving
    async def test_a_video_shows_the_toolbox_with_every_tool(self):
        status, rid = await self.upload()
        self.assertIn("Toolbox", status.text)
        self.assertIn("0:06", status.text)
        self.assertIn("320×180", status.text)
        shown = labels(status.markup)
        for label in ("✄ Trim", "♪ Extract audio", "⇩ Compress", "◍ GIF", "◧ Burn subtitles", "⌫ Remove metadata", "✕ Close"):
            self.assertIn(label, shown)
        self.assertTrue(all(d.endswith(rid) for d in datas(status.markup)))
        self.assertTrue(all(len(d.encode()) < 64 for d in datas(status.markup)))

    async def test_audio_gets_only_the_tools_that_make_sense(self):
        status = await self.send(audio=Media(SOUND, "song.mp3"))
        shown = labels(status.markup)
        self.assertIn("✄ Trim", shown)
        self.assertIn("♪ Extract audio", shown)
        for label in ("⇩ Compress", "◍ GIF", "◧ Burn subtitles"):
            self.assertNotIn(label, shown)

    async def test_a_silent_video_has_no_extract_audio(self):
        status = await self.send(video=Media(SILENT, "mute.mp4"))
        self.assertNotIn("♪ Extract audio", labels(status.markup))

    async def test_documents_are_accepted_only_when_they_are_media(self):
        media = Media(VIDEO, "movie.mkv")
        media.mime_type = "video/x-matroska"
        status = await self.send(document=media)
        self.assertIn("Toolbox", status.text)
        pdf = Media(VIDEO, "report.pdf")
        pdf.mime_type = "application/pdf"
        self.assertIsNone(await self.send(document=pdf))

    async def test_files_that_cannot_be_read_or_fetched_are_explained_and_cleaned_up(self):
        junk = _TMP / "junk.bin"
        junk.write_bytes(b"nope nope nope")
        status = await self.send(video=Media(junk, "x.mp4"))
        self.assertIn("doesn't look like", status.text)
        status = await self.send(video=Media(VIDEO, "x.mp4", fail=True))
        self.assertIn("couldn't download", status.text)
        self.assertEqual(list(tools.store._items), [])
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_huge_files_are_refused_up_front(self):
        message = Incoming(video=Media(VIDEO, "big.mp4", size=3 * 1024 ** 3))
        update = FakeUpdate(self.bot)
        update.message = message
        sent = []

        async def reply_text(text, **kw):
            sent.append(text)
        message.reply_text = reply_text
        await main.media_handler(update, FakeContext(self.bot))
        self.assertIn("2 GB", sent[0])
        self.assertEqual(list(tools.store._items), [])

    async def test_a_person_keeps_at_most_three_waiting_uploads(self):
        for _ in range(5):
            await self.send(video=Media(VIDEO, "a.mp4"))
        self.assertEqual(len(tools.store.for_user(USER)), main.MAX_STORED_UPLOADS_PER_USER)
        self.assertEqual(len(list(self.root.iterdir())), main.MAX_STORED_UPLOADS_PER_USER)

    # -- the local Bot API server's copy of what was sent
    def server_copy(self, folder, name, source=VIDEO):
        import config
        root = Path(tempfile.mkdtemp(prefix="botapi-root-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(setattr, config, "BOT_API_DATA_DIR", config.BOT_API_DATA_DIR)
        config.BOT_API_DATA_DIR = str(root)
        path = root / "123:ABC" / folder / name
        path.parent.mkdir(parents=True)
        shutil.copy(source, path)
        return path

    async def test_the_servers_copy_of_an_upload_is_deleted_once_we_have_ours(self):
        server = self.server_copy("videos", "file_1.mp4")
        media = Media(VIDEO, "holiday.mp4")
        media.file_path = str(server)
        status = await self.send(video=media)
        self.assertIn("Toolbox", status.text)
        self.assertFalse(server.exists())
        self.assertTrue(tools.store.get(next(iter(tools.store._items)), USER).path.is_file())

    async def test_the_servers_copy_of_an_unreadable_upload_is_deleted_too_or_at_least_not_kept_by_us(self):
        server = self.server_copy("videos", "file_2.mp4")
        media = Media(VIDEO, "x.mp4")
        media.file_path = str(server)
        await self.send(video=media)
        self.assertFalse(server.exists())

    async def test_the_servers_copy_of_a_subtitle_file_is_deleted(self):
        _, rid = await self.upload()
        await self.tap(f"tl|burn|{rid}")
        server = self.server_copy("documents", "file_3.srt", source=SUBS)
        document = Media(SUBS, "subs.srt")
        document.file_path = str(server)
        update = FakeUpdate(self.bot)
        update.message = Incoming(document=document)
        await main.srt_handler(update, FakeContext(self.bot))
        self.assertFalse(server.exists())
        self.assertEqual(self.jobs.enqueued[-1]["settings"]["tool"], "burn")

    async def test_the_servers_copy_of_a_cookies_file_is_deleted_it_is_a_secret(self):
        import config
        cookies_dir = Path(tempfile.mkdtemp(prefix="cookies-"))
        self.addCleanup(shutil.rmtree, cookies_dir, ignore_errors=True)
        self.addCleanup(setattr, config, "COOKIES_DIR", config.COOKIES_DIR)
        config.COOKIES_DIR = str(cookies_dir)
        self.addCleanup(setattr, main, "update_setting", main.update_setting)
        self.addCleanup(setattr, main, "inspect_cookie_file", main.inspect_cookie_file)
        main.update_setting = lambda *a, **k: None
        main.inspect_cookie_file = lambda path: {"netscape": True, "youtube": False, "logged_in": False, "expired": False}
        cookies = _TMP / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\tabc\n")
        server = self.server_copy("documents", "file_4.txt", source=cookies)
        document = Media(cookies, "cookies.txt")
        document.file_path = str(server)
        update = FakeUpdate(self.bot)
        update.message = Incoming(document=document)
        await main.cookies_file_handler(update, FakeContext(self.bot))
        self.assertTrue((cookies_dir / f"{USER}.txt").is_file())
        self.assertFalse(server.exists())

    # -- one-tap tools
    async def test_extract_audio_asks_for_a_format_then_queues_the_job(self):
        _, rid = await self.upload()
        edit = await self.tap(f"tl|aud|menu|{rid}")
        self.assertEqual(labels(edit["markup"])[:2], ["MP3", "M4A"])
        await self.tap(f"tl|aud|m4a|{rid}")
        job = self.jobs.enqueued[-1]
        self.assertEqual((job["settings"]["tool"], job["settings"]["audio_format"], job["url"]), ("audio", "m4a", "tool://audio"))
        self.assertEqual(job["title"], "holiday.mp4")
        self.assertEqual(job["settings"]["tool_input"], str(tools.store.get(rid, USER).path))
        self.assertFalse(job["settings"]["adhd_mode"])
        self.assertIn(rid, main.last_download)                                   # so "Try again" works

    async def test_strip_queues_straight_away(self):
        _, rid = await self.upload()
        await self.tap(f"tl|strip|{rid}")
        self.assertEqual(self.jobs.enqueued[-1]["settings"]["tool"], "strip")

    async def test_a_second_tap_while_the_file_is_being_worked_on_does_not_start_another_job(self):
        _, rid = await self.upload()
        self.jobs.active.add(rid)
        edit = await self.tap(f"tl|strip|{rid}")
        self.assertEqual(self.jobs.enqueued, [])
        self.assertIn("Already working", edit["text"])

    # -- compress
    async def test_compress_offers_only_sizes_smaller_than_the_file(self):
        _, rid = await self.upload()
        edit = await self.tap(f"tl|cmpm|{rid}")
        self.assertNotIn("10 MB", labels(edit["markup"]))                         # the sample is smaller than 10 MB
        self.assertIn("← Back", labels(edit["markup"]))

    async def test_a_size_the_video_cannot_reach_is_refused_with_a_reason_and_nothing_is_queued(self):
        _, rid = await self.upload()
        item = tools.store.get(rid, USER)
        item.info.size, item.info.duration = 500_000_000, 4 * 3600
        edit = await self.tap(f"tl|cmp|10|{rid}")
        self.assertIn("too long", edit["text"])
        self.assertEqual(self.jobs.enqueued, [])

    async def test_a_fabricated_size_is_ignored(self):
        _, rid = await self.upload()
        await self.tap(f"tl|cmp|7|{rid}")
        self.assertEqual(self.jobs.enqueued, [])

    async def test_a_size_that_fits_is_queued(self):
        _, rid = await self.upload()
        tools.store.get(rid, USER).info.size = 500_000_000
        await self.tap(f"tl|cmp|50|{rid}")
        self.assertEqual(self.jobs.enqueued[-1]["settings"]["target_mb"], 50)

    # -- trim
    async def test_trim_asks_for_times_then_for_fast_or_exact_then_queues(self):
        _, rid = await self.upload()
        edit = await self.tap(f"tl|trim|{rid}")
        self.assertIn("Send the <b>start</b>", edit["text"])
        edit = await self.type_text("0:01 0:04")
        self.assertIn("0:01 → 0:04", edit["text"])
        self.assertEqual(labels(edit["markup"])[:2], ["⚡ Fast", "✄ Exact"])
        await self.tap(f"tl|trimgo|exact|{rid}")
        settings = self.jobs.enqueued[-1]["settings"]
        self.assertEqual((settings["tool"], settings["start"], settings["end"], settings["exact"]), ("trim", 1.0, 4.0, True))

    async def test_trimming_audio_has_no_fast_or_exact_choice(self):
        status = await self.send(audio=Media(SOUND, "song.mp3"))
        rid = next(iter(tools.store._items))
        await self.tap(f"tl|trim|{rid}")
        edit = await self.type_text("0:01 0:03")
        self.assertEqual(labels(edit["markup"])[0], "✄ Trim")

    async def test_a_typo_keeps_waiting_with_the_reason(self):
        _, rid = await self.upload()
        await self.tap(f"tl|trim|{rid}")
        edit = await self.type_text("banana")
        self.assertIn("⚠", edit["text"])
        self.assertIn(USER, main.pending_tool_input)
        edit = await self.type_text("0:01 0:02")
        self.assertIn("0:01 → 0:02", edit["text"])
        self.assertNotIn(USER, main.pending_tool_input)

    async def test_a_link_pasted_while_waiting_moves_on(self):
        _, rid = await self.upload()
        await self.tap(f"tl|trim|{rid}")
        before = len(self.bot.edits)
        try:
            await main.link_handler(FakeUpdate(self.bot, text="no link here"), FakeContext(self.bot))
        except Exception:
            pass
        self.assertGreater(len(self.bot.edits), before)                          # that was the trim answer (a typo)
        main.pending_tool_input[USER] = {"rid": rid, "kind": "trim", "chat_id": 7, "message_id": 99, "at": 0}
        await main.link_handler(FakeUpdate(self.bot, text="0:01 0:02"), FakeContext(self.bot))
        self.assertNotIn(USER, main.pending_tool_input)                          # stale: dropped, not used
        self.assertNotIn(rid, main.tool_drafts)

    async def test_pressing_fast_or_exact_without_times_asks_for_them(self):
        _, rid = await self.upload()
        edit = await self.tap(f"tl|trimgo|fast|{rid}")
        self.assertIn("Send the times again", edit["text"])
        self.assertEqual(self.jobs.enqueued, [])

    # -- gif
    async def test_gif_takes_a_start_and_a_length_and_queues_immediately(self):
        _, rid = await self.upload()
        await self.tap(f"tl|gif|{rid}")
        await self.type_text("0:01 3")
        settings = self.jobs.enqueued[-1]["settings"]
        self.assertEqual((settings["tool"], settings["start"], settings["length"]), ("gif", 1.0, 3.0))

    async def test_a_gif_that_is_too_long_is_refused(self):
        _, rid = await self.upload()
        await self.tap(f"tl|gif|{rid}")
        edit = await self.type_text("0:00 60")
        self.assertIn("at most", edit["text"])
        self.assertEqual(self.jobs.enqueued, [])

    # -- burn
    async def srt_message(self, content=SRT.encode(), name="subs.srt", size=None):
        document = Media(SUBS, name)
        document.file_size = size if size is not None else len(content)
        path = _TMP / "incoming.srt"
        path.write_bytes(content)
        document.source = path
        update = FakeUpdate(self.bot)
        update.message = Incoming(document=document)
        await main.srt_handler(update, FakeContext(self.bot))
        return update.message

    async def test_burn_waits_for_the_srt_then_queues_with_it(self):
        _, rid = await self.upload()
        edit = await self.tap(f"tl|burn|{rid}")
        self.assertIn(".srt", edit["text"])
        await self.srt_message()
        settings = self.jobs.enqueued[-1]["settings"]
        self.assertEqual(settings["tool"], "burn")
        self.assertTrue(Path(settings["tool_srt"]).is_file())
        self.assertNotIn(USER, main.pending_tool_input)

    async def test_an_srt_in_old_persian_encoding_is_converted(self):
        _, rid = await self.upload()
        await self.tap(f"tl|burn|{rid}")
        await self.srt_message("1\r\n00:00:01,000 --> 00:00:02,000\r\nسلام خوب\r\n".encode("cp1256"))
        self.assertIn("سلام خوب", Path(self.jobs.enqueued[-1]["settings"]["tool_srt"]).read_text(encoding="utf-8"))

    async def test_a_bad_srt_keeps_waiting(self):
        _, rid = await self.upload()
        await self.tap(f"tl|burn|{rid}")
        await self.srt_message(b"hello there")
        self.assertEqual(self.jobs.enqueued, [])
        self.assertEqual(main.pending_tool_input[USER]["kind"], "srt")
        self.assertIn("⚠", self.bot.edits[-1]["text"])

    async def test_an_srt_nobody_asked_for_gets_a_hint_not_a_job(self):
        message = await self.srt_message()
        self.assertEqual(self.jobs.enqueued, [])
        self.assertIn("send the video first", message.replies[-1].text)

    async def test_burn_is_refused_for_very_long_videos(self):
        _, rid = await self.upload()
        tools.store.get(rid, USER).info.duration = 10 * 3600
        edit = await self.tap(f"tl|burn|{rid}")
        self.assertIn("too long", edit["text"])
        self.assertNotIn(USER, main.pending_tool_input)

    # -- housekeeping and safety
    async def test_close_forgets_the_file_and_frees_the_disk(self):
        _, rid = await self.upload()
        await self.tap(f"tl|x|{rid}")
        self.assertIsNone(tools.store.get(rid, USER))
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_an_expired_or_foreign_file_cannot_be_used(self):
        _, rid = await self.upload()
        item = tools.store._items[rid]
        item.user_id = 999                                                        # somebody else's upload
        edit = await self.tap(f"tl|strip|{rid}")
        self.assertIn("expired", edit["text"])
        self.assertEqual(self.jobs.enqueued, [])

    async def test_back_returns_to_the_toolbox_and_abandons_typing(self):
        _, rid = await self.upload()
        await self.tap(f"tl|trim|{rid}")
        self.assertIn(USER, main.pending_tool_input)
        edit = await self.tap(f"tl|home|{rid}")
        self.assertIn("Toolbox", edit["text"])
        self.assertNotIn(USER, main.pending_tool_input)

    async def test_the_stale_state_sweep_drops_old_prompts_and_uploads(self):
        _, rid = await self.upload()
        main.pending_tool_input[USER] = {"rid": rid, "kind": "trim", "chat_id": 1, "message_id": 1, "at": 0}
        main.tool_drafts[rid] = {"start": 0, "end": 1}
        main._rid_last_touch[rid] = 0
        tools.store._items[rid].at = 0
        main._sweep_stale_state_once()
        self.assertEqual((main.pending_tool_input, main.tool_drafts), ({}, {}))
        self.assertIsNone(tools.store.get(rid, USER))

    def test_the_start_menu_and_intro_offer_tools(self):
        from ui.start_menu import start_menu
        self.assertIn("🧰 Tools", labels(start_menu()))
        self.assertIn("misc|tools", datas(start_menu()))
        self.assertIn("send me a video or audio file", tools_menu.TOOLS_INTRO.lower())
        self.assertEqual(datas(tools_menu.tools_intro_menu()), ["nav|home"])


class MiscMenu(unittest.IsolatedAsyncioTestCase):
    async def test_tools_on_the_start_menu_opens_the_intro_with_a_way_back(self):
        bot = FakeBot()

        async def allow(*a, **k):
            return True
        main.gate_callback = allow
        await main.misc_callback(FakeUpdate(bot, FakeQuery("misc|tools", bot)), FakeContext(bot))
        self.assertIn("Tools", bot.last["text"])
        self.assertEqual(datas(bot.last["markup"]), ["nav|home"])


# ============================================================ delivery through the job manager
class TelegramBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []
        self.deleted: list = []
        self.edited: list = []

    def _record(self, kind, kw, file):
        self.sent.append({"kind": kind, "markup": kw.get("reply_markup"), "caption": kw.get("caption", ""),
                          "filename": getattr(file, "filename", None)})

    async def send_video(self, chat_id, file, **kw):
        self._record("video", kw, file)

    async def send_audio(self, chat_id, file, **kw):
        self._record("audio", kw, file)

    async def send_animation(self, chat_id, file, **kw):
        self._record("animation", kw, file)

    async def send_document(self, chat_id, file, **kw):
        self._record("document", kw, file)

    async def send_photo(self, chat_id, file, **kw):
        self._record("photo", kw, file)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_message_text(self, text, **kw):
        self.edited.append((text, kw.get("reply_markup"), kw.get("message_id")))

    async def send_message(self, *a, **kw):
        pass


class Delivery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tooljob-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bot = TelegramBot()
        self.manager = jm.JobManager(self.bot, max_concurrent=1)
        self.edits: list = []
        self.addCleanup(setattr, tools, "store", tools.store)
        tools.store = tools.InputStore(self.tmp / "uploads")
        self.addCleanup(setattr, jm, "job_workspace", jm.job_workspace)

        from contextlib import contextmanager

        @contextmanager
        def workspace():
            folder = self.tmp / "ws"
            folder.mkdir(exist_ok=True)
            yield folder
        jm.job_workspace = workspace

    def job(self, tool, source=VIDEO, **extra):
        path = tools.store.folder(RID) / f"original{source.suffix}"
        shutil.copy(source, path)
        info = probe_sync(path)
        tools.store.add(RID, 1, path, source.name, info)
        settings = {**DEFAULTS, "tool": tool, "tool_input": str(path), "tool_name": source.name,
                    "tool_duration": info.duration, "tool_has_video": info.has_video, "tool_height": info.height, **extra}
        return jm.Job(rid=RID, user_id=1, chat_id=7, url=f"tool://{tool}", settings=settings, status_message_id=55,
                      header="Queued", title=source.name)

    async def test_a_finished_tool_job_delivers_the_file_without_a_source_link_or_send_as_file_button(self):
        job = self.job("strip")
        await self.manager._run_job(job)
        [sent] = self.bot.sent
        self.assertEqual(sent["kind"], "video")
        self.assertIsNone(sent["markup"])                                         # nothing to copy, nothing cached
        self.assertNotIn("compressed preview", sent["caption"])
        self.assertEqual(self.bot.deleted, [])                                    # the first message is NOT removed ...
        text, markup, message_id = self.bot.edited[-1]
        self.assertEqual(message_id, 55)
        self.assertIn("Toolbox", text)                                            # ... it turns back into the toolbox
        self.assertIn("tl|strip|" + RID, datas(markup))
        self.assertIsNotNone(tools.store.get(RID, 1))                             # and the original stays for more tools

    async def test_results_sent_as_documents_carry_no_buttons_either(self):
        mkv = self.tmp / "clip [clean].avi"
        mkv.write_bytes(b"x" * 100)
        job = self.job("strip")
        await self.manager._send_files(job, [mkv])
        self.assertEqual((self.bot.sent[0]["kind"], self.bot.sent[0]["markup"]), ("document", None))
        job.force_document = True
        await self.manager._send_files(job, [mkv])
        self.assertEqual((self.bot.sent[1]["kind"], self.bot.sent[1]["markup"]), ("document", None))

    async def test_the_clock_restarts_after_each_result_so_the_file_lives_an_hour_from_last_use(self):
        job = self.job("strip")
        ten_minutes_ago = time.time() - 600
        tools.store._items[RID].at = ten_minutes_ago
        await self.manager._run_job(job)
        self.assertGreater(tools.store._items[RID].at, ten_minutes_ago + 500)

    async def test_several_tools_can_run_on_the_same_upload_one_after_another(self):
        await self.manager._run_job(self.job("strip"))
        again = jm.Job(rid=RID, user_id=1, chat_id=7, url="tool://audio",
                       settings={**DEFAULTS, "tool": "audio", "audio_format": "mp3", "tool_input": str(tools.store.get(RID, 1).path),
                                 "tool_name": "video.mp4", "tool_duration": 6, "tool_has_video": True, "tool_height": 180},
                       status_message_id=56, header="Queued", title="video.mp4")
        await self.manager._run_job(again)
        self.assertEqual([s["kind"] for s in self.bot.sent], ["video", "audio"])

    async def test_if_the_upload_is_gone_the_progress_message_is_removed_instead(self):
        job = self.job("strip")
        original = media_tools_run = jm.media_tools.run

        async def run_then_forget(settings, workspace, cb, cancel_event):
            files = await original(settings, workspace, cb, cancel_event)
            tools.store.discard(RID)                        # e.g. the person pressed Close meanwhile
            return files
        self.addCleanup(setattr, jm.media_tools, "run", original)
        jm.media_tools.run = run_then_forget
        await self.manager._run_job(job)
        self.assertEqual(self.bot.deleted, [55])

    async def test_a_tool_jobs_bar_is_one_line_updated_in_place(self):
        job = self.job("audio", audio_format="mp3")

        async def ticking(settings, workspace, cb, cancel_event):
            for percent in (5, 17, 29, 41, 53, 100):
                cb(percent, "17.8x", None, None, "Extracting audio")
                await asyncio.sleep(0)
            out = workspace / "a.mp3"
            out.write_bytes(b"x" * 10)
            return [out]
        self.addCleanup(setattr, jm.media_tools, "run", jm.media_tools.run)
        jm.media_tools.run = ticking
        await self.manager._run_job(job)
        lines = [step for step in job.steps if "Extracting audio" in step]
        self.assertEqual(len(lines), 1, job.steps)
        self.assertIn("100%", lines[0])

    async def test_a_gif_is_sent_as_an_animation(self):
        await self.manager._run_job(self.job("gif", start=0, length=2))
        self.assertEqual(self.bot.sent[0]["kind"], "animation")

    async def test_extracted_audio_is_sent_as_audio(self):
        await self.manager._run_job(self.job("audio", audio_format="mp3"))
        self.assertEqual(self.bot.sent[0]["kind"], "audio")
        self.assertIsNone(self.bot.sent[0]["markup"])

    async def test_a_failure_keeps_the_original_so_try_again_works(self):
        job = self.job("burn", tool_srt=str(self.tmp / "gone.srt"))
        with self.assertRaises(tools.ToolError):
            await self.manager._run_job(job)
        self.assertIsNotNone(tools.store.get(RID, 1))

    async def test_tool_jobs_are_not_logged_as_downloads(self):
        calls = []
        self.addCleanup(setattr, jm.ac, "log_download", jm.ac.log_download)
        jm.ac.log_download = lambda *a, **k: calls.append(a)
        self.manager._log_history(self.job("strip"), "success")
        self.assertEqual(calls, [])

    async def test_failure_menus_have_no_empty_rows_when_there_is_no_link(self):
        from ui.quick_menu import retry_menu, sent_menu, send_as_file_menu
        for markup in (retry_menu(RID, "tool://strip"), sent_menu("tool://strip"), send_as_file_menu(RID, "tool://strip")):
            self.assertTrue(all(row for row in markup.inline_keyboard) if hasattr(markup, "inline_keyboard") else True)
        self.assertEqual(labels(retry_menu(RID, "tool://strip")), ["↻ Try again", "✕ Delete"])
        self.assertEqual(labels(retry_menu(RID, "https://youtu.be/x"))[-1], "🔗 Video link")


if __name__ == "__main__":
    unittest.main()
