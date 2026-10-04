"""
Estimated download sizes for the quality buttons ("720p ~45MB").

Pure functions - no yt-dlp / telegram imports. The estimate deliberately
follows the SAME preferences as ytdlp_handler._format_selector (H.264 video +
AAC audio when available), so the number describes what would really be
fetched, not some other rendition.

Sizes come from what the site announces: an exact filesize, else an
approximate one, else bitrate x duration. They are estimates (YouTube's own
numbers are approximate, and merging adds a little overhead), which is why the
buttons show a "~".
"""

MP3_KBPS = 192           # the bot's default audio bitrate (settings audio_bitrate)
OPUS_FALLBACK_KBPS = 128  # when there is no native Opus stream to copy


def _size(fmt: dict, duration: float | None) -> int | None:
    exact = fmt.get("filesize") or fmt.get("filesize_approx")
    if exact:
        return int(exact)
    if fmt.get("tbr") and duration:
        return int(fmt["tbr"] * 125 * duration)      # kbit/s -> bytes/s is x125
    return None


def _has(fmt: dict, key: str) -> bool:
    return fmt.get(key) not in (None, "none")


def _split(formats: list[dict]):
    video_only = [f for f in formats if _has(f, "vcodec") and not _has(f, "acodec") and f.get("height")]
    audio_only = [f for f in formats if _has(f, "acodec") and not _has(f, "vcodec")]
    muxed = [f for f in formats if _has(f, "vcodec") and _has(f, "acodec") and f.get("height")]
    return video_only, audio_only, muxed


def _quality_key(fmt: dict) -> tuple:
    return (fmt.get("tbr") or fmt.get("abr") or 0, fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def _best_audio(audio_only: list[dict]) -> dict | None:
    """What 'bestaudio[acodec^=mp4a]/bestaudio' would pick."""
    aac = [f for f in audio_only if str(f.get("acodec", "")).startswith("mp4a")]
    pool = aac or audio_only
    return max(pool, key=_quality_key) if pool else None


def _video_at(video_only: list[dict], height: int) -> dict | None:
    """What 'bestvideo[height<=h][vcodec^=avc1]/bestvideo[height<=h]' picks:
    the tallest H.264 video up to h if there is any, otherwise the tallest of
    anything up to h. (Not simply "the tallest": a 4K VP9-only rendition must
    not be quoted when the bot would fetch the 1080p H.264 one.)"""
    at_most = [f for f in video_only if f["height"] <= height]
    avc = [f for f in at_most if str(f.get("vcodec", "")).startswith("avc1")]
    pool = avc or at_most
    if not pool:
        return None
    top = max(f["height"] for f in pool)
    return max((f for f in pool if f["height"] == top), key=_quality_key)


def estimate_sizes(formats: list[dict], duration: float | None, heights: list[int]) -> dict[str, int]:
    """{"best": n, "worst": n, "mp3": n, "opus": n, "1080": n, ...} in bytes.
    A key is simply absent when it can't be estimated (no sizes announced)."""
    video_only, audio_only, muxed = _split(formats)
    audio = _best_audio(audio_only)
    audio_size = _size(audio, duration) if audio else None
    result: dict[str, int] = {}

    def at_height(height: int) -> int | None:
        video = _video_at(video_only, height)
        if video is not None:
            video_size = _size(video, duration)
            return video_size + audio_size if video_size is not None and audio_size is not None else None
        same = [f for f in muxed if f["height"] == height]      # sites that only offer combined files
        return _size(max(same, key=_quality_key), duration) if same else None

    for height in heights:
        size = at_height(height)
        if size is not None:
            result[str(height)] = size
    best = at_height(10**9)      # no height cap: same rule as the "best" button
    if best is not None:
        result["best"] = best

    # "Smallest size" = worstvideo+worstaudio: the lowest-resolution video and the smallest audio.
    if video_only and audio_only:
        low = min(video_only, key=lambda f: (f["height"], _quality_key(f)))
        quiet = min(audio_only, key=_quality_key)
        low_size, quiet_size = _size(low, duration), _size(quiet, duration)
        if low_size is not None and quiet_size is not None:
            result["worst"] = low_size + quiet_size

    if duration:
        result["mp3"] = int(duration * MP3_KBPS * 125)
        opus = [f for f in audio_only if str(f.get("acodec", "")).startswith("opus")]
        opus_size = _size(max(opus, key=_quality_key), duration) if opus else None
        result["opus"] = opus_size if opus_size is not None else int(duration * OPUS_FALLBACK_KBPS * 125)
    return result


def format_size(num_bytes: float) -> str:
    """Compact for a narrow button: 850KB, 45MB, 1.2GB."""
    if num_bytes < 999_500:                      # rounds to under 1000KB
        return f"{max(1, round(num_bytes / 1000))}KB"
    if num_bytes < 999_500_000:
        value = num_bytes / 1_000_000
        return f"{value:.0f}MB" if value >= 9.95 else f"{value:.1f}MB"
    return f"{num_bytes / 1_000_000_000:.1f}GB"


def size_labels(sizes: dict[str, int], scale: float = 1.0) -> dict[str, str]:
    """Display strings for the buttons, e.g. {"720": "~45MB"}. `scale` shrinks
    them to the share of the video being downloaded when time-range sections
    are chosen."""
    return {key: f"~{format_size(value * scale)}" for key, value in sizes.items() if value > 0}
