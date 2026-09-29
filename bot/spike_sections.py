"""
Spike: can we download ONLY a time range of a long YouTube video, inside the
real bot environment? (Cookies, PO-token provider, real format selector.)

Throwaway diagnostic - it is not imported by the bot. Run it INSIDE the bot
container so it sees the same cookies, PO-token provider, Deno and ffmpeg:

    docker compose cp ./bot/spike_sections.py bot:/app/spike_sections.py
    docker compose exec bot python spike_sections.py "https://www.youtube.com/watch?v=XXXX" \
        --section 1:50:00-1:52:00 --section 0:10:00-0:11:00

Use a genuinely long video (1-2 hours). The whole point is to measure that a
2-minute clip transfers roughly 2 minutes' worth of data.

Tests (pick with --tests, default A,B,C,D):
  A  one section, one yt-dlp run             (the basic case)
  B  two sections, one yt-dlp run EACH       (our planned design)
  C  two sections in ONE yt-dlp call         (yt-dlp issue #8756 - do they collide?)
  D  one section, audio only (mp3)           (audio clips)

Output contains no cookies or tokens; safe to paste back. It is also saved to
/tmp/spike/report.txt inside the container.

Sections are START-END; either side may be empty:  "1:50:00-1:52:00", "-2:00",
"1:50:00-".  Timestamps: H:MM:SS, MM:SS or plain seconds.
"""
import argparse
import copy
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPORT_LINES: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    REPORT_LINES.append(text)


# ---------- pure helpers (no yt-dlp needed) ----------

def parse_ts(text: str) -> float:
    """'1:50:00' / '50:00' / '110' / '90.5' -> seconds. Raises ValueError."""
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp {text!r}")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)   # float() rejects garbage for us
    if total < 0:
        raise ValueError("negative timestamp")
    return total


def parse_section(arg: str) -> tuple[float, float | None]:
    """'START-END' -> (start_s, end_s). Empty start = 0, empty end = None (to the end)."""
    if "-" not in arg:
        raise argparse.ArgumentTypeError(f"section {arg!r} must look like START-END, e.g. 1:50:00-1:52:00")
    left, right = arg.split("-", 1)
    try:
        start = parse_ts(left) if left.strip() else 0.0
        end = parse_ts(right) if right.strip() else None
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    if end is not None and end <= start:
        raise argparse.ArgumentTypeError(f"section {arg!r}: end must be after start")
    return start, end


def fmt_hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_mb(n: float) -> str:
    return f"{n / 1_000_000:.1f} MB"


def rx_bytes() -> int:
    """Total bytes received by this container (all interfaces except lo).
    Used to measure REAL transfer, independent of what yt-dlp claims."""
    total = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            total += int(data.split()[0])
    except (OSError, ValueError, IndexError):
        return -1
    return total


def ffmpeg_summaries() -> list[str]:
    """One short line per running ffmpeg: only its cut/encode flags (the input
    URLs are long, signed and pointless to print). Reads /proc directly since
    the slim image has no ps/pgrep."""
    wanted = {"-ss", "-t", "-to", "-c:v", "-c:a", "-c", "-preset", "-crf"}
    found = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or b"ffmpeg" not in argv[0]:
            continue
        args = [a.decode("utf-8", "replace") for a in argv]
        flags = [f"{a} {args[i + 1]}" for i, a in enumerate(args[:-1]) if a in wanted]
        found.append(" ".join(flags) or "(no cut flags)")
    return found


def heartbeat(workdir: Path, stop: threading.Event, rx0: int, t0: float) -> None:
    """Prints a status line every 10s while yt-dlp works, so a slow run is
    distinguishable from a hung one (it used to sit silent for minutes)."""
    while not stop.wait(10):
        rx = rx_bytes()
        got = fmt_mb(rx - rx0) if rx >= 0 and rx0 >= 0 else "?"
        size = sum(f.stat().st_size for f in workdir.rglob("*") if f.is_file())
        procs = ffmpeg_summaries()
        say(f"  [{time.time() - t0:5.0f}s] downloaded so far: {got} | files on disk: {fmt_mb(size)} | "
            f"ffmpeg: {'; '.join(procs) if procs else 'not running'}")


def ffprobe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        return {"error": str(e)}
    streams = data.get("streams", [])
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return {
        "duration": duration,
        "video": next((s for s in streams if s.get("codec_type") == "video"), None),
        "audio": next((s for s in streams if s.get("codec_type") == "audio"), None),
    }


# ---------- progress-event recorder ----------

class Recorder:
    """Keeps what yt-dlp's hooks emit, so we learn whether a clip download
    reports usable progress (bytes/percent) or goes silent."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def progress(self, d: dict) -> None:
        self.events.append(("progress", self._slim(d)))

    def postproc(self, d: dict) -> None:
        self.events.append(("postproc", self._slim(d)))

    @staticmethod
    def _slim(d: dict) -> dict:
        keep = ("status", "downloaded_bytes", "total_bytes", "total_bytes_estimate", "speed", "eta",
                "elapsed", "fragment_index", "fragment_count", "postprocessor")
        slim = {k: d[k] for k in keep if k in d and d[k] is not None}
        info = d.get("info_dict") or {}
        slim["fmt"] = f"{info.get('format_id')}/{info.get('vcodec')}/{info.get('acodec')}"
        if d.get("filename"):
            slim["file"] = Path(d["filename"]).name[-40:]
        return slim

    def summary(self) -> list[str]:
        lines = []
        prog = [e for k, e in self.events if k == "progress"]
        post = [e for k, e in self.events if k == "postproc"]
        lines.append(f"progress events: {len(prog)}, postprocessor events: {len(post)}")
        if prog:
            statuses: dict[str, int] = {}
            for e in prog:
                statuses[e.get("status", "?")] = statuses.get(e.get("status", "?"), 0) + 1
            lines.append(f"  progress statuses: {statuses}")
            has_total = any("total_bytes" in e or "total_bytes_estimate" in e for e in prog)
            has_dl = any("downloaded_bytes" in e for e in prog)
            lines.append(f"  usable for a percent bar?  total known: {has_total}, downloaded_bytes present: {has_dl}")
            for label, chunk in (("first", prog[:2]), ("last", prog[-2:])):
                for e in chunk:
                    lines.append(f"  {label}: {e}")
        if post:
            names = sorted({e.get("postprocessor", "?") for e in post})
            lines.append(f"  postprocessors seen: {names}")
        return lines


# ---------- bot-environment glue ----------

def build_env(url: str, user_id: int, mode: str, quality: str, use_cookies: bool, fmt: str | None):
    """Import the bot's real option builder so the spike matches production."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config  # noqa: WPS433
    from downloader.ytdlp_handler import _build_opts
    from settings.user_settings import get_settings

    s = dict(get_settings(user_id))
    s["mode"] = mode
    s["quality"] = quality
    cookie_path = Path(config.COOKIES_DIR) / f"{user_id}.txt"
    s["cookies_enabled"] = bool(use_cookies and cookie_path.exists())
    return s, _build_opts, s["cookies_enabled"], cookie_path.exists(), fmt


def probe_info(url, s, build_opts, user_id, fmt):
    import yt_dlp
    opts = build_opts(url, Path("/tmp/spike/probe"), s, user_id, lambda d: None, lambda d: None)
    if fmt:
        opts["format"] = fmt
    opts["noplaylist"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    duration = info.get("duration") or 0
    fmts = info.get("requested_formats") or [info]
    total = 0.0
    for f in fmts:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and duration:
            size = f["tbr"] * 1000 / 8 * duration
        total += size or 0
    return info, duration, total


def run_download(name, url, s, build_opts, user_id, ranges, precise, fmt, outroot, template):
    """One yt-dlp run. `ranges` = list of (start, end|None). Returns result dict."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    workdir = outroot / name
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    rec = Recorder()
    st = copy.deepcopy(s)
    st["filename_template"] = template
    opts = build_opts(url, workdir, st, user_id, rec.progress, rec.postproc)
    if fmt:
        opts["format"] = fmt
    opts["noplaylist"] = True
    opts["download_ranges"] = download_range_func(None, [(a, b if b is not None else float("inf")) for a, b in ranges])
    opts["force_keyframes_at_cuts"] = precise

    t0, rx0 = time.time(), rx_bytes()
    error = None
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(workdir, stop, rx0, t0), daemon=True).start()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except KeyboardInterrupt:
        error = "interrupted with Ctrl+C"
    except Exception as e:  # noqa: BLE001 - we want to report anything
        error = f"{type(e).__name__}: {str(e)[:400]}"
    finally:
        stop.set()
    elapsed, rx1 = time.time() - t0, rx_bytes()

    files = sorted(p for p in workdir.rglob("*") if p.is_file())
    return {"name": name, "workdir": workdir, "files": files, "elapsed": elapsed,
            "rx": (rx1 - rx0) if rx0 >= 0 and rx1 >= 0 else None, "error": error, "rec": rec,
            "ranges": ranges, "precise": precise, "opts_format": opts["format"]}


def report(res: dict, duration: float, full_size: float, expect_video: bool) -> str:
    """Print one test's findings; return PASS / WARN / FAIL."""
    say(f"\n--- {res['name']} ---")
    say(f"format selector: {res['opts_format']}")
    say(f"precise cuts (force_keyframes_at_cuts): {res['precise']}")
    say(f"wall time: {res['elapsed']:.1f}s")
    if res["error"]:
        say(f"ERROR: {res['error']}")

    clip_len = sum(((b if b is not None else duration) - a) for a, b in res["ranges"])
    rx = res["rx"]
    verdict = "PASS"
    if rx is None:
        say("network transfer: could not measure (/proc/net/dev unreadable)")
    else:
        say(f"network transferred (container RX): {fmt_mb(rx)}")
        if full_size and duration:
            expected = full_size * clip_len / duration
            say(f"  whole video would be ~{fmt_mb(full_size)}; expected for these clips ~{fmt_mb(expected)} "
                f"({rx / max(expected, 1):.1f}x expected, {rx / full_size * 100:.1f}% of the full video)")
            if rx > expected * 3 + 8_000_000:
                say("  !! transferred far more than clip-sized - range download may not be saving bandwidth")
                verdict = "WARN"
        else:
            say("  (full-video size unknown, cannot compare)")

    media = [f for f in res["files"] if f.suffix.lower() not in (".part", ".ytdl", ".json", ".webp", ".jpg", ".png")]
    leftovers = [f for f in res["files"] if f.suffix.lower() in (".part", ".ytdl")]
    say(f"output files ({len(media)}):")
    for f in media:
        say(f"  {f.name}  [{fmt_mb(f.stat().st_size)}]")
    if leftovers:
        say(f"  leftover temp files: {[f.name for f in leftovers]}")
        verdict = "WARN"

    if not media:
        say("!! NO OUTPUT FILE")
        return "FAIL"
    if res["error"]:
        verdict = "FAIL"

    for f in media:
        if f.stat().st_size == 0:
            say(f"!! {f.name} is EMPTY (yt-dlp issue #9328 symptom)")
            verdict = "FAIL"
            continue
        info = ffprobe(f)
        if "error" in info:
            say(f"  ffprobe failed on {f.name}: {info['error']}")
            verdict = "FAIL"
            continue
        v, a = info["video"], info["audio"]
        say(f"  ffprobe {f.name[-30:]}: duration={info['duration']}s "
            f"video={v and v.get('codec_name')} {v and v.get('width')}x{v and v.get('height')} "
            f"audio={a and a.get('codec_name')}")
        if expect_video and not v:
            say("  !! no video stream")
            verdict = "FAIL"
        if not a:
            say("  !! no audio stream")
            verdict = "FAIL"

    if len(res["ranges"]) == 1 and len(media) == 1:
        a, b = res["ranges"][0]
        want = (b if b is not None else duration) - a
        got = ffprobe(media[0]).get("duration")
        if got is not None:
            delta = got - want
            say(f"  clip length: wanted {want:.1f}s, got {got:.1f}s (off by {delta:+.1f}s)")
            tolerance = 1.5 if res["precise"] else 8.0
            if abs(delta) > tolerance:
                say(f"  !! outside the +/-{tolerance:.1f}s tolerance for this cut mode")
                verdict = "WARN" if verdict == "PASS" else verdict
    elif len(res["ranges"]) > 1 and len(media) != len(res["ranges"]):
        say(f"  !! asked for {len(res['ranges'])} sections in one call, got {len(media)} file(s) "
            f"- sections collided or merged (issue #8756)")
        verdict = "FAIL" if len(media) < len(res["ranges"]) else verdict

    for line in res["rec"].summary():
        say(line)
    say(f"RESULT {res['name']}: {verdict}")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--section", action="append", type=parse_section, required=True,
                    help="START-END, repeatable (give at least 2 to run tests B and C)")
    ap.add_argument("--tests", default="A,B,C,D", help="comma list from A,B,C,D")
    ap.add_argument("--user-id", type=int, default=None, help="whose cookies to use (default: OWNER_USER_ID)")
    ap.add_argument("--no-cookies", action="store_true", help="ignore the cookies file")
    ap.add_argument("--fast", action="store_true", help="skip force_keyframes_at_cuts (faster, less exact)")
    ap.add_argument("--quality", default="best", help="e.g. best, 1080, 720 (as in the bot's picker)")
    ap.add_argument("--format", default=None, help="override yt-dlp format selector (to test alternatives)")
    ap.add_argument("--keep", action="store_true", help="keep downloaded clips in /tmp/spike")
    args = ap.parse_args()

    try:
        import yt_dlp
    except ImportError:
        print("yt_dlp not importable - run this inside the bot container (docker compose exec bot ...)")
        return 2

    import config
    user_id = args.user_id if args.user_id is not None else config.OWNER_USER_ID
    tests = [t.strip().upper() for t in args.tests.split(",") if t.strip()]
    precise = not args.fast
    quality = args.quality if args.quality.endswith("p") or args.quality in ("best", "worst") else f"{args.quality}p"
    sections = args.section
    outroot = Path("/tmp/spike")
    outroot.mkdir(parents=True, exist_ok=True)

    say(f"yt-dlp {yt_dlp.version.__version__}   deno: {shutil.which('deno')}   ffmpeg: {shutil.which('ffmpeg')}")
    s, build_opts, cookies_on, cookies_exist, fmt = build_env(args.url, user_id, "video", quality,
                                                                not args.no_cookies, args.format)
    say(f"user_id={user_id}  cookies file exists: {cookies_exist}  cookies used: {cookies_on}")
    say(f"precise cuts: {precise}   sections: {[(fmt_hms(a), fmt_hms(b) if b else 'end') for a, b in sections]}")

    say("\n=== probing the video ===")
    try:
        info, duration, full_size = probe_info(args.url, s, build_opts, user_id, fmt)
    except Exception as e:  # noqa: BLE001
        say(f"PROBE FAILED: {type(e).__name__}: {str(e)[:500]}")
        say("(if this is the 'page needs to be reloaded' / sign-in error, the clip test can't run yet)")
        return 1
    say(f"title: {info.get('title')}")
    say(f"duration: {fmt_hms(duration)}  ({duration}s)   live: {info.get('is_live')}")
    say(f"estimated full download for the chosen format: {fmt_mb(full_size) if full_size else 'unknown'}")
    for a, b in sections:
        if a >= duration or (b is not None and b > duration + 1):
            say(f"!! section {fmt_hms(a)}-{fmt_hms(b) if b else 'end'} is outside the video length")
    if duration < 1800:
        say("!! this video is short - the bandwidth comparison is only convincing on a long one (1h+)")

    verdicts: dict[str, str] = {}
    label = lambda a, b: f"{fmt_hms(a).replace(':', '-')}_{fmt_hms(b).replace(':', '-') if b else 'end'}"

    if "A" in tests:
        say("\n=== TEST A: one section, one run ===")
        a, b = sections[0]
        res = run_download("A_single", args.url, s, build_opts, user_id, [(a, b)], precise, fmt, outroot,
                           f"%(title).50B [A {label(a, b)}].%(ext)s")
        verdicts["A"] = report(res, duration, full_size, expect_video=True)

    if "B" in tests:
        if len(sections) < 2:
            say("\n=== TEST B skipped: give at least two --section values ===")
        else:
            say("\n=== TEST B: one run PER section (our planned design) ===")
            results = []
            for i, (a, b) in enumerate(sections, 1):
                res = run_download(f"B_clip{i}", args.url, s, build_opts, user_id, [(a, b)], precise, fmt,
                                   outroot, f"%(title).50B [clip{i} {label(a, b)}].%(ext)s")
                results.append(report(res, duration, full_size, expect_video=True))
            verdicts["B"] = "FAIL" if "FAIL" in results else ("WARN" if "WARN" in results else "PASS")

    if "C" in tests:
        if len(sections) < 2:
            say("\n=== TEST C skipped: give at least two --section values ===")
        else:
            say("\n=== TEST C: several sections in ONE yt-dlp call (known-buggy upstream) ===")
            res = run_download("C_multi_one_call", args.url, s, build_opts, user_id, sections, precise, fmt,
                               outroot, "%(title).50B.%(ext)s")
            verdicts["C"] = report(res, duration, full_size, expect_video=True)
            say("(C failing is NOT a blocker - it just confirms we must use one run per section, as in B.)")

    if "D" in tests:
        say("\n=== TEST D: one section, audio only (mp3) ===")
        sa = copy.deepcopy(s)
        sa["mode"] = "audio"
        sa["audio_format"] = "mp3"
        a, b = sections[0]
        res = run_download("D_audio", args.url, sa, build_opts, user_id, [(a, b)], precise, args.format,
                           outroot, f"%(title).50B [audio {label(a, b)}].%(ext)s")
        verdicts["D"] = report(res, duration, full_size, expect_video=False)

    say("\n=== SUMMARY ===")
    for k in sorted(verdicts):
        say(f"  test {k}: {verdicts[k]}")
    (outroot / "report.txt").write_text("\n".join(REPORT_LINES), encoding="utf-8")
    say(f"\n(full report saved to {outroot / 'report.txt'} inside the container)")

    if not args.keep:
        for d in outroot.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
    return 0 if all(v != "FAIL" for k, v in verdicts.items() if k != "C") else 1


if __name__ == "__main__":
    raise SystemExit(main())
