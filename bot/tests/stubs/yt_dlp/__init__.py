"""
Test double for yt_dlp (the real one needs network + a real video). Only what
the clip code touches. Behaviour is scripted per call through SCRIPT:

    "ok"        write a real, tiny clip with ffmpeg, of the requested length
    "fail"      raise a generic error
    "botcheck"  raise YouTube's "confirm you're not a bot" error
    "slow"      like "ok" but takes ~0.5s first (a download with no progress info)
    "hang"      behave like a stuck ffmpeg child: start a process NAMED ffmpeg
                that mentions the output folder, wait for it, and fail the way
                yt-dlp does when that child gets killed
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import utils  # noqa: F401  (yt_dlp.utils.X is used by the code under test)

REAL_FFMPEG = shutil.which("ffmpeg")
SCRIPT: list[str] = []
CALLS: list[dict] = []
EXTRACT_CALLS: list[tuple] = []     # (url, opts) for every extract_info
TITLE = "A long title cut mid-sentence "     # trailing space, like %(title).60B can leave


# When enabled, an "ok" clip first writes ffmpeg-style -progress blocks (if the
# caller asked for them via external_downloader_args), one run per stream,
# exactly as ffmpeg does: each run restarts the file, blocks end in
# progress=continue|end.
PROGRESS = {"enabled": False, "streams": 2, "pause": 0.2}

# When enabled, a plain download writes a REAL audio file (ffmpeg-generated, with a 16:9 cover like a
# YouTube thumbnail) instead of a 100-byte placeholder.
PLAIN_AUDIO = {"enabled": False, "suffix": "mp3", "seconds": 30, "cover": True}

# Progress-hook events a plain (non-clip) download should emit, in order.
HOOK_EVENTS: list[dict] = []
# What extract_info() returns / how long it takes (for the preview lookup).
EXTRACT = {"result": {"title": "T", "formats": []}, "delay": 0.0, "by_url": {}}


def reset(script: list[str]) -> None:
    SCRIPT[:] = script
    CALLS.clear()
    HOOK_EVENTS.clear()
    PLAIN_AUDIO.update(enabled=False, suffix="mp3", seconds=30, cover=True)
    PROGRESS.update(enabled=False, streams=2, pause=0.2)
    EXTRACT.update(result={"title": "T", "formats": []}, delay=0.0, by_url={})
    EXTRACT_CALLS.clear()


def _write_audio(path):
    """A real audio file of the configured length, with an attached 640x360 cover when asked."""
    seconds, codec = PLAIN_AUDIO["seconds"], {"mp3": "libmp3lame", "opus": "libopus"}[PLAIN_AUDIO["suffix"]]
    tone = [REAL_FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:d={seconds}"]
    if PLAIN_AUDIO["cover"] and PLAIN_AUDIO["suffix"] == "mp3":
        cover = path.with_suffix(".cover.jpg")
        subprocess.run([REAL_FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=1",
                        "-frames:v", "1", str(cover)], check=True)
        subprocess.run([*tone, "-i", str(cover), "-map", "0:a", "-map", "1", "-c:a", codec, "-c:v", "copy",
                        "-id3v2_version", "3", "-metadata", "comment=a long video description",
                        "-disposition:v", "attached_pic", str(path)], check=True)
        cover.unlink()
    else:
        subprocess.run([*tone, "-c:a", codec, str(path)], check=True)


def _block(out_us, status):
    value = "N/A" if out_us is None else str(out_us)
    return f"frame=1\ntotal_size=0\nout_time_us={value}\nout_time_ms={value}\nspeed=1.0x\nprogress={status}\n"


def _simulate_ffmpeg_progress(opts, length_seconds):
    args = (opts.get("external_downloader_args") or {}).get("ffmpeg_i") or []
    if not PROGRESS["enabled"] or "-progress" not in args:
        return
    path = Path(args[args.index("-progress") + 1])
    for _ in range(PROGRESS["streams"]):
        with open(path, "w") as fh:                      # a new ffmpeg run starts the file over
            fh.write(_block(None, "continue"))
            for fraction in (0.25, 0.6):
                fh.write(_block(int(length_seconds * 1e6 * fraction), "continue"))
                fh.flush()
                time.sleep(PROGRESS["pause"])
            fh.write(_block(int(length_seconds * 1e6), "end"))
            fh.flush()
            time.sleep(PROGRESS["pause"])


class YoutubeDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        EXTRACT_CALLS.append((url, dict(self.opts)))
        time.sleep(EXTRACT["delay"])
        outcome = EXTRACT["by_url"].get(url, EXTRACT["result"])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def download(self, urls):
        index = len(CALLS)
        CALLS.append(self.opts)
        action = SCRIPT[index] if index < len(SCRIPT) else "ok"
        target = Path(self.opts["outtmpl"].replace("%(title).60B", TITLE).replace("%(ext)s", "mp4"))
        target.parent.mkdir(parents=True, exist_ok=True)

        if action == "ppfail":                      # what yt-dlp raises when an ffmpeg step fails
            logger = self.opts.get("logger")
            if logger:
                logger.debug("[debug] ffmpeg command line: ffmpeg -y -i in.webm -c:a libopus out.opus")
                logger.debug("[ffmpeg] Invalid argument for option b:a")
                logger.debug("[download] something unrelated")
            raise Exception("ERROR: Postprocessing: Conversion failed!")
        if action == "fail":
            raise Exception("boom")
        if action == "botcheck":
            raise Exception("Sign in to confirm you’re not a bot")
        if action == "hang":
            fake = Path(tempfile.mkdtemp()) / "ffmpeg"
            fake.symlink_to(sys.executable)
            child = subprocess.Popen([str(fake), "-c", "import time; time.sleep(60)", str(target.parent / "x.mp4")])
            code = child.wait()
            raise Exception(f"ffmpeg exited with code {code}")

        if "download_ranges" not in self.opts:                     # a normal, whole-video download
            if PLAIN_AUDIO["enabled"]:
                _write_audio(target.parent / f"song.{PLAIN_AUDIO['suffix']}")
            else:
                (target.parent / "video.mp4").write_bytes(b"x" * 100)
            for event in HOOK_EVENTS:
                for hook in self.opts.get("progress_hooks", []):
                    hook(event)
            return
        section = self.opts["download_ranges"]({}, self)[0]
        length = section["end_time"] - section["start_time"]
        if action == "slow":
            time.sleep(0.5)
        _simulate_ffmpeg_progress(self.opts, length)
        subprocess.run(
            [REAL_FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=d={length}:s=64x48:r=5",
             "-f", "lavfi", "-i", f"sine=d={length}", "-shortest", "-pix_fmt", "yuv420p", str(target)],
            check=True,
        )
        for hook in self.opts.get("progress_hooks", []):
            hook({"status": "finished", "info_dict": {}})
