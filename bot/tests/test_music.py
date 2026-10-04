"""Music done properly: tags, a square cover, one track per chapter."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _env  # noqa: F401  (must come first)

import yt_dlp  # noqa: E402  (the stub)
from downloader import music, ytdlp_handler  # noqa: E402
from downloader.errors import JobCancelled  # noqa: E402
from downloader.music import clean_title, music_tags, track_filename, valid_chapters  # noqa: E402
from settings.user_settings import DEFAULTS  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
HAVE_OPUS = HAVE_FFMPEG and b"libopus" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                                         capture_output=True).stdout


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height:stream_disposition=attached_pic:stream_tags:format=duration:format_tags",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    data = json.loads(out)
    cover = next((s for s in data.get("streams", []) if s["codec_type"] == "video"), None)
    return {"duration": float(data["format"]["duration"]), "tags": {k.lower(): v for k, v in data["format"].get("tags", {}).items()},
            "cover": cover, "stream_tags": {k.lower(): v for s in data["streams"] for k, v in s.get("tags", {}).items()}}


# ============================================================ pure logic
class Titles(unittest.TestCase):
    def test_noise_is_stripped_from_the_end_only(self):
        for raw, clean in [("Song (Official Video)", "Song"), ("Song [Lyrics]", "Song"), ("Song (Official Music Video) [HD]", "Song"),
                           ("Song (Remastered 2011)", "Song"), ("Song (Official Audio)", "Song"), ("Song (Live at the Garden)", "Song (Live at the Garden)"),
                           ("(Official) Song", "(Official) Song"), ("  spaced   out  ", "spaced out"), ("", "")]:
            with self.subTest(raw=raw):
                self.assertEqual(clean_title(raw), clean)


class Tags(unittest.TestCase):
    def test_music_uploads_use_their_real_fields(self):
        info = {"track": "Real Song", "artist": "Real Artist", "album": "Real Album", "release_year": 2019,
                "title": "Real Artist - Real Song (Official Video)", "uploader": "Real Artist - Topic"}
        self.assertEqual(music_tags(info), {"title": "Real Song", "artist": "Real Artist", "album": "Real Album", "date": "2019"})

    def test_artist_dash_title_is_split(self):
        self.assertEqual(music_tags({"title": "Daft Punk - One More Time (Official Video)", "uploader": "Some Channel"}),
                         {"title": "One More Time", "artist": "Daft Punk"})

    def test_other_dashes_and_en_dashes(self):
        self.assertEqual(music_tags({"title": "Artist – Song"}), {"title": "Song", "artist": "Artist"})
        self.assertEqual(music_tags({"title": "Re-mix of a song"}), {"title": "Re-mix of a song"})      # a hyphen without spaces is not a separator

    def test_no_dash_means_the_channel_is_the_artist_and_topic_is_dropped(self):
        self.assertEqual(music_tags({"title": "Just a title", "uploader": "Chan - Topic", "upload_date": "20210305"}),
                         {"title": "Just a title", "artist": "Chan", "date": "2021"})
        self.assertEqual(music_tags({"title": "T", "channel": "Chan"}), {"title": "T", "artist": "Chan"})

    def test_empty_info_gives_no_tags(self):
        self.assertEqual(music_tags({}), {})


class Names(unittest.TestCase):
    def test_track_filenames_are_safe_and_numbered(self):
        self.assertEqual(track_filename(1, "Intro", ".mp3"), "01 - Intro.mp3")
        self.assertEqual(track_filename(12, 'a/b\\c:d*e?f"g<h>i|j', ".opus"), "12 - a b c d e f g h i j.opus")
        self.assertEqual(track_filename(3, "", ".mp3"), "03 - Track.mp3")
        self.assertEqual(track_filename(4, "x" * 200, ".mp3"), "04 - " + "x" * 80 + ".mp3")
        self.assertEqual(track_filename(5, "ends with dots...", ".mp3"), "05 - ends with dots.mp3")


class Chapters(unittest.TestCase):
    def test_cleanup(self):
        info = {"duration": 100, "chapters": [
            {"title": "One", "start_time": 0, "end_time": 40},
            {"title": "  ", "start_time": 40, "end_time": 70},                 # untitled
            {"title": "Last", "start_time": 70, "end_time": 100.8},            # end clamped to the length
            {"title": "tiny", "start_time": 99, "end_time": 99.5},             # under a second: dropped
            {"title": "junk", "start_time": "x", "end_time": 5},
            {"title": "no end", "start_time": 90},                            # runs to the end
        ]}
        got = valid_chapters(info)
        self.assertEqual([c[0] for c in got], ["One", "Track 2", "Last", "no end"])
        self.assertEqual(got[2][1:], (70.0, 100.0))
        self.assertEqual(got[3][1:], (90.0, 100.0))
        self.assertEqual(valid_chapters({}), [])


# ============================================================ real ffmpeg
def make_mp3(directory: Path, seconds=30, cover=True, name="song.mp3") -> Path:
    yt_dlp.reset([])
    yt_dlp.PLAIN_AUDIO.update(enabled=True, suffix="mp3", seconds=seconds, cover=cover)
    path = directory / name
    yt_dlp._write_audio(path)
    return path


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class Polishing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="music-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    async def test_writes_tags_and_squares_the_cover_without_touching_the_audio(self):
        path = make_mp3(self.tmp)
        before = probe(path)
        self.assertEqual((before["cover"]["width"], before["cover"]["height"]), (640, 360))
        self.assertTrue(await music.polish_mp3(path, {"title": "Real Title", "artist": "The Artist", "album": "Alb", "date": "2019"}))
        after = probe(path)
        self.assertEqual((after["cover"]["width"], after["cover"]["height"]), (360, 360))        # centre-cropped square
        self.assertEqual(after["cover"]["disposition"]["attached_pic"], 1)
        self.assertEqual({k: after["tags"][k] for k in ("title", "artist", "album", "date")},
                         {"title": "Real Title", "artist": "The Artist", "album": "Alb", "date": "2019"})
        self.assertAlmostEqual(after["duration"], before["duration"], delta=0.1)
        self.assertEqual(list(self.tmp.glob("*.polished.mp3")), [])                           # no temp file left

    async def test_a_file_without_a_cover_still_gets_its_tags(self):
        path = make_mp3(self.tmp, cover=False)
        self.assertTrue(await music.polish_mp3(path, {"title": "T", "artist": "A"}))
        after = probe(path)
        self.assertIsNone(after["cover"])
        self.assertEqual(after["tags"]["title"], "T")

    async def test_a_broken_file_is_left_exactly_as_it_was(self):
        path = self.tmp / "broken.mp3"
        path.write_bytes(b"this is not audio")
        self.assertFalse(await music.polish_mp3(path, {"title": "T"}))
        self.assertEqual(path.read_bytes(), b"this is not audio")
        self.assertEqual(list(self.tmp.glob("*.polished.mp3")), [])

    async def test_cancel_stops_ffmpeg(self):
        path = make_mp3(self.tmp)
        cancel = asyncio.Event()
        cancel.set()
        with self.assertRaises(JobCancelled):
            await music.polish_mp3(path, {"title": "T"}, cancel)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class Splitting(unittest.IsolatedAsyncioTestCase):
    CHAPTERS = [("Intro", 0.0, 10.0), ("Second: song?", 10.0, 20.0), ("Finale", 20.0, 30.0)]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="music-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    async def test_one_tagged_track_per_chapter_with_the_cover(self):
        source = make_mp3(self.tmp)
        await music.polish_mp3(source, {"title": "Whole mix", "artist": "DJ", "album": "Summer"})
        tracks = await music.split_by_chapters(source, self.CHAPTERS, {"artist": "DJ", "album": "Summer"})
        self.assertEqual([t.name for t in tracks], ["01 - Intro.mp3", "02 - Second song.mp3", "03 - Finale.mp3"])
        for number, (track, (title, start, end)) in enumerate(zip(tracks, self.CHAPTERS), 1):
            info = probe(track)
            self.assertAlmostEqual(info["duration"], end - start, delta=0.3)
            self.assertEqual((info["tags"]["title"], info["tags"]["track"], info["tags"]["artist"], info["tags"]["album"]),
                             (title, f"{number}/3", "DJ", "Summer"))
            self.assertEqual((info["cover"]["width"], info["cover"]["height"]), (360, 360))       # cover kept on every track
            self.assertNotIn("comment", info["tags"])                                              # the video's description isn't copied

    async def test_duplicate_titles_get_distinct_files(self):
        source = make_mp3(self.tmp)
        tracks = await music.split_by_chapters(source, [("Same", 0.0, 5.0), ("Same", 5.0, 10.0)], {})
        self.assertEqual(len({t.name for t in tracks}), 2)

    async def test_a_failure_cleans_up_and_reports_empty(self):
        source = self.tmp / "broken.mp3"
        source.write_bytes(b"nope")
        self.assertEqual(await music.split_by_chapters(source, self.CHAPTERS, {}), [])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["broken.mp3"])

    @unittest.skipUnless(HAVE_OPUS, "needs libopus")
    async def test_opus_is_split_too(self):
        yt_dlp.reset([])
        yt_dlp.PLAIN_AUDIO.update(enabled=True, suffix="opus", seconds=20, cover=False)
        source = self.tmp / "mix.opus"
        yt_dlp._write_audio(source)
        tracks = await music.split_by_chapters(source, [("A", 0.0, 10.0), ("B", 10.0, 20.0)], {"artist": "DJ"})
        self.assertEqual([t.suffix for t in tracks], [".opus", ".opus"])
        info = probe(tracks[1])
        self.assertAlmostEqual(info["duration"], 10, delta=0.3)
        self.assertEqual((info["stream_tags"].get("title"), info["stream_tags"].get("track")), ("B", "2/2"))


# ============================================================ through the real download path
@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg + ffprobe")
class ThroughTheHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="music-ws-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.stages: list[str] = []
        yt_dlp.reset([])
        yt_dlp.PLAIN_AUDIO.update(enabled=True, suffix="mp3", seconds=30, cover=True)

    def event(self, **info):
        return {"status": "finished", "downloaded_bytes": 1, "total_bytes": 1, "filename": "song.webm",
                "info_dict": {"format_id": "251", "vcodec": "none", "acodec": "opus", **info}}

    async def download(self, info, **settings):
        yt_dlp.HOOK_EVENTS[:] = [self.event(**info)]
        full = dict(DEFAULTS, mode="audio", audio_format="mp3", **settings)

        def cb(*args):
            if len(args) > 3 and args[3]:
                self.stages.append(args[3])
        files = await ytdlp_handler.download("https://youtu.be/x", self.workspace, full, 1, cb)
        await asyncio.sleep(0)
        return files

    async def test_an_ordinary_upload_gets_clean_tags_and_a_square_cover(self):
        files = await self.download({"title": "Daft Punk - One More Time (Official Video)", "uploader": "Chan"})
        self.assertEqual([f.name for f in files], ["song.mp3"])
        info = probe(files[0])
        self.assertEqual((info["tags"]["title"], info["tags"]["artist"]), ("One More Time", "Daft Punk"))
        self.assertEqual((info["cover"]["width"], info["cover"]["height"]), (360, 360))

    async def test_split_by_chapters_returns_the_tracks_instead_of_the_whole_file(self):
        chapters = [{"title": "Alpha", "start_time": 0, "end_time": 10}, {"title": "Beta", "start_time": 10, "end_time": 20},
                    {"title": "Gamma", "start_time": 20, "end_time": 30}]
        files = await self.download({"title": "Summer mix 2024", "uploader": "DJ X", "duration": 30, "chapters": chapters},
                                    split_chapters=True)
        self.assertEqual([f.name for f in files], ["01 - Alpha.mp3", "02 - Beta.mp3", "03 - Gamma.mp3"])
        self.assertIn("Splitting into 3 tracks", self.stages)
        self.assertFalse((self.workspace / "song.mp3").exists())                      # the whole file is gone
        tags = probe(files[1])["tags"]
        self.assertEqual((tags["title"], tags["track"], tags["album"], tags["artist"]), ("Beta", "2/3", "Summer mix 2024", "DJ X"))

    async def test_split_without_chapters_sends_the_whole_file_and_says_so(self):
        files = await self.download({"title": "No chapters here", "duration": 30}, split_chapters=True)
        self.assertEqual([f.name for f in files], ["song.mp3"])
        self.assertIn("No chapters to split by - sending the whole file", self.stages)

    async def test_too_many_chapters_are_not_split(self):
        chapters = [{"title": f"T{i}", "start_time": i * 0.5, "end_time": i * 0.5 + 0.5} for i in range(60)]
        # (chapters this short are dropped, so use longer ones on a longer file)
        yt_dlp.PLAIN_AUDIO["seconds"] = 130
        chapters = [{"title": f"T{i}", "start_time": i * 2, "end_time": i * 2 + 2} for i in range(music.MAX_TRACKS + 5)]
        files = await self.download({"title": "Huge", "duration": 130, "chapters": chapters}, split_chapters=True)
        self.assertEqual(len(files), 1)
        self.assertIn("No chapters to split by - sending the whole file", self.stages)

    async def test_without_the_split_flag_nothing_is_split(self):
        chapters = [{"title": "A", "start_time": 0, "end_time": 15}, {"title": "B", "start_time": 15, "end_time": 30}]
        files = await self.download({"title": "T", "duration": 30, "chapters": chapters})
        self.assertEqual(len(files), 1)
        self.assertNotIn("Splitting", " ".join(self.stages))

    async def test_video_downloads_and_other_audio_formats_are_left_alone(self):
        yt_dlp.PLAIN_AUDIO["enabled"] = False
        yt_dlp.HOOK_EVENTS[:] = [self.event(title="A - B")]
        video = await ytdlp_handler.download("https://youtu.be/x", self.workspace, dict(DEFAULTS, mode="video", quality="best"), 1, lambda *a: None)
        self.assertEqual([f.name for f in video], ["video.mp4"])
        files = [self.workspace / "a.m4a"]
        out = await ytdlp_handler._finish_audio(files, {"title": "A - B"}, {"audio_format": "m4a"}, lambda *a: None, None)
        self.assertIs(out, files)
        two = [self.workspace / "a.mp3", self.workspace / "b.mp3"]
        out = await ytdlp_handler._finish_audio(two, {"title": "A - B"}, {"audio_format": "mp3"}, lambda *a: None, None)
        self.assertIs(out, two)                                                       # a playlist is left as downloaded

    async def test_a_problem_in_the_finishing_step_never_loses_the_download(self):
        original = ytdlp_handler.music_tags
        ytdlp_handler.music_tags = lambda info: 1 / 0
        self.addCleanup(setattr, ytdlp_handler, "music_tags", original)
        files = await self.download({"title": "A - B"})
        self.assertEqual([f.name for f in files], ["song.mp3"])


if __name__ == "__main__":
    unittest.main()
