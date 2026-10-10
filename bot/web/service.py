"""
What the web app does on a person's behalf, written as plain async functions (no HTTP in here): previewing a link,
turning the person's choices into job settings, the settings whitelist, cookies, and toolbox uploads.
The same engine as the bot: same probe, same dispatcher, same JobManager.
"""
import os
import re
from pathlib import Path

import config
from downloader import cookie_health, tools
from downloader import cookies as cookie_lib
from downloader import gallerydl_probe
from downloader.playlist import ListingError, MAX_LISTED, list_playlist, looks_like_playlist, quick_titles, short_label
from downloader.probe import probe
from downloader.sections import SectionError, parse_timestamp, validate_sections
from downloader.site_map import is_image_site, tool_order_for
from downloader.subtitles import MAX_LANGS, MODES, SubtitleError
from settings.user_settings import DEFAULTS, get_settings
from utils.text import site_label

URL_RE = re.compile(r"^https?://[^\s<>\"']{4,2000}$", re.IGNORECASE)
MAX_URLS_PER_BATCH = 25
MAX_UPLOAD_BYTES = config.MAX_FILE_SIZE_BYTES


class WebError(Exception):
    """A problem with the request; the message is for the person (plain text) and the status is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def clean_url(raw) -> str:
    url = str(raw or "").strip()
    if not URL_RE.match(url):
        raise WebError("That doesn't look like a link (it must start with http:// or https://).")
    return url


# ------------------------------------------------------------------ preview
def _track(track) -> dict:
    return {"code": track.code, "name": track.name, "auto": bool(track.auto)}


async def preview(url: str, user_id: int) -> dict:
    """Everything the page needs to offer choices for one link. Mirrors the bot's link flow."""
    url = clean_url(url)
    settings = get_settings(user_id)
    order = tool_order_for(url)
    base = {"url": url, "site": site_label(url), "kind": "fallback", "title": "", "thumbnail": "", "heights": [],
            "has_audio": True, "duration": None, "sizes": {}, "chapters": [], "subtitles": [], "entries": [],
            "note": "", "show_sizes": bool(settings.get("show_sizes", True))}
    if "spotify.com" in url:
        return {**base, "kind": "spotify"}

    async def playlist() -> dict:
        try:
            title, entries = await list_playlist(url, user_id)
        except ListingError as exc:
            raise WebError(str(exc), 502)
        return {**base, "kind": "playlist", "title": title,
                "entries": [{"url": e.url, "title": e.title, "duration": e.duration} for e in entries],
                "note": f"Showing the first {MAX_LISTED} videos." if len(entries) >= MAX_LISTED else ""}

    if order[0] == "ytdlp" and looks_like_playlist(url):
        return await playlist()

    result = await probe(url, user_id) if "ytdlp" in order else None
    if result is not None and cookie_health.cookies_in_use(user_id, settings, url):
        cookie_health.watch.record_success(user_id) if result.ok else cookie_health.watch.record_failure(user_id, result.error)
    if result and result.ok and result.is_playlist:
        return await playlist()
    if result and result.ok:
        kind = "video" if result.heights else ("audio" if result.has_audio else "simple")
        return {**base, "kind": kind, "title": result.title, "thumbnail": result.thumbnail,
                "heights": result.heights, "has_audio": result.has_audio, "duration": result.duration,
                "sizes": result.sizes if base["show_sizes"] else {},
                "chapters": [{"title": t, "start": s, "end": e} for t, s, e in result.chapters],
                "subtitles": [_track(t) for t in result.subtitles]}

    info = await gallerydl_probe.probe(url) if "gallerydl" in order else None
    found = {"title": (info or {}).get("title", ""), "thumbnail": (info or {}).get("thumbnail", "")}
    if order[0] != "ytdlp" or is_image_site(url):
        return {**base, **found, "kind": "simple"}
    error = (result.error if result else "") or ""
    return {**base, **found, "kind": "fallback",
            "note": "I couldn't read details for this link (" + (error[:160] or "unknown reason") + "). "
                    "You can still try downloading it."}


# ------------------------------------------------------------------ the person's choices -> job settings
QUALITIES = ("best", "worst", "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p")
AUDIO_CHOICES = ("mp3", "opus", "m4a", "flac", "wav", "mp3split")


def _sections(raw, duration) -> list[tuple[float, float]]:
    out = []
    for item in raw or []:
        if not isinstance(item, (list, tuple, dict)):
            raise WebError("A section looks wrong - please add it again.")
        start, end = (item.get("start"), item.get("end")) if isinstance(item, dict) else (item[0], item[1])
        try:
            start = parse_timestamp(str(start)) if str(start or "").strip() else None
            end = parse_timestamp(str(end)) if str(end or "").strip() else None
        except SectionError as exc:
            raise WebError(str(exc))
        out.append((start, end))
    try:
        return validate_sections(out, duration)
    except SectionError as exc:
        raise WebError(str(exc))


def build_settings(user_id: int, req: dict) -> tuple[str, dict]:
    """(url, job settings) for a download request. Validates everything: the page is not trusted."""
    url = clean_url(req.get("url"))
    kind = req.get("kind", "video")
    if kind not in ("video", "audio", "simple"):
        raise WebError("Unknown download type.")
    settings = dict(get_settings(user_id))
    settings["adhd_mode"] = False
    if kind == "video":
        quality = str(req.get("quality", "best"))
        if quality not in QUALITIES:
            raise WebError("Unknown quality.")
        settings.update(mode="video", quality=quality)
    elif kind == "audio":
        fmt = str(req.get("audio_format", "mp3"))
        if fmt not in AUDIO_CHOICES:
            raise WebError("Unknown audio format.")
        settings["mode"] = "audio"
        if fmt == "mp3split":
            settings.update(audio_format="mp3", split_chapters=True)
        else:
            settings["audio_format"] = fmt
    sections = req.get("sections") or []
    duration = req.get("duration")
    duration = float(duration) if isinstance(duration, (int, float)) and duration > 0 else None
    if sections and kind != "simple":
        settings["sections"] = _sections(sections, duration)
        settings["sections_merge"] = bool(req.get("merge")) and len(settings["sections"]) > 1
        settings.pop("split_chapters", None)
    subs = req.get("subs") or {}
    if subs.get("langs") and kind == "video" and not sections:
        langs = [str(code)[:20] for code in subs["langs"]][:MAX_LANGS]
        mode = str(subs.get("mode", "embed"))
        if mode not in MODES or not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,20}", code) for code in langs):
            raise WebError("Unknown subtitle option.")
        settings.update(sub_langs=langs, sub_mode=mode)
    if req.get("had_preview") and tool_order_for(url)[0] != "ytdlp":
        settings["prefer_ytdlp"] = True
    elif kind == "simple" and is_image_site(url):
        settings["prefer_gallerydl"] = True
    return url, settings


def batch_settings(user_id: int, url: str, quality: str) -> dict:
    settings = dict(get_settings(user_id))
    settings["adhd_mode"] = False
    if quality in ("mp3", "opus"):
        settings["mode"], settings["audio_format"] = "audio", quality
    elif quality in QUALITIES:
        settings["mode"], settings["quality"] = "video", quality
    else:
        raise WebError("Unknown quality.")
    if tool_order_for(url)[0] == "spotify":
        settings["mode"] = "audio"
    return settings


async def titles_for(urls: list[str], user_id: int) -> list[dict]:
    lookup = [u for u in urls if tool_order_for(u)[0] == "ytdlp"]
    found = {e.url: e for e in await quick_titles(lookup, user_id)} if lookup else {}
    return [{"url": u, "title": (found[u].title if u in found else short_label(u)),
             "duration": found[u].duration if u in found else None} for u in urls]


# ------------------------------------------------------------------ settings
_BOOL = {"embed_thumbnail", "embed_metadata", "sponsorblock", "use_archive", "show_sizes", "cookies_enabled"}
_CHOICES = {
    "mode": ("video", "audio"), "quality": ("best", "worst", "1080p", "720p", "480p", "360p"),
    "video_codec": ("any", "h264", "vp9", "av1"), "audio_format": ("mp3", "m4a", "opus", "flac", "wav"),
    "audio_bitrate": ("128", "192", "256", "320"), "playlist_mode": ("single", "full", "range"),
    "bar_style": ("auto", "candy", "jar", "pacman", "slider", "moon"),
}
# "proxy" and "filename_template" are deliberately NOT editable from the web: a proxy would let a page-supplied
# address be dialled from the server, and a template could write outside the job's folder.
WEB_SETTINGS = sorted({*_BOOL, *_CHOICES, "playlist_range", "rate_limit_kbps", "concurrent_fragments"})


def settings_view(user_id: int) -> dict:
    current = get_settings(user_id)
    return {key: current[key] for key in WEB_SETTINGS if key in current}


def clean_setting(key: str, value):
    if key not in WEB_SETTINGS or key not in DEFAULTS:
        raise WebError("That setting can't be changed here.")
    if key in _BOOL:
        if not isinstance(value, bool):
            raise WebError("That setting is on or off.")
        return value
    if key in _CHOICES:
        if str(value) not in _CHOICES[key]:
            raise WebError("That isn't one of the choices.")
        return str(value)
    if key == "playlist_range":
        text = str(value or "").strip()
        if text and not re.fullmatch(r"\d{1,4}(-\d{1,4})?", text):
            raise WebError("Use a range like 1-5.")
        return text
    if key in ("rate_limit_kbps", "concurrent_fragments"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise WebError("That needs a whole number.")
        low, high = (0, 1_000_000) if key == "rate_limit_kbps" else (1, 16)
        if not low <= value <= high:
            raise WebError(f"Use a number from {low} to {high}.")
        return value
    raise WebError("That setting can't be changed here.")


# ------------------------------------------------------------------ cookies (one file per person, merged per site)
MAX_COOKIE_BYTES = 1_000_000


def cookie_path(user_id: int) -> Path:
    return Path(config.COOKIES_DIR) / f"{user_id}.txt"


def cookie_sites(user_id: int) -> list[dict]:
    return [{"site": s.site, "label": cookie_lib.site_label(s.site), "count": s.count, "expired": s.expired}
            for s in cookie_lib.list_sites(cookie_path(user_id))]


def save_cookies(user_id: int, data: bytes) -> dict:
    if not data or len(data) > MAX_COOKIE_BYTES:
        raise WebError("That file is empty or too big for a cookies.txt.")
    Path(config.COOKIES_DIR).mkdir(parents=True, exist_ok=True)
    dest = cookie_path(user_id)
    upload = dest.with_suffix(".upload")
    fd = os.open(upload, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    try:
        check = cookie_lib.inspect_cookie_file(upload)
        merged = cookie_lib.merge_upload(dest, upload)
    finally:
        upload.unlink(missing_ok=True)
    if merged is None:
        raise WebError("That doesn't look like a cookies.txt export (Netscape format), so nothing was changed.")
    from settings.user_settings import update_setting
    update_setting(user_id, "cookies_enabled", True)
    warnings = []
    if check["youtube"] and not check["logged_in"]:
        warnings.append("There's no YouTube login in this file, so it won't help with YouTube.")
    elif check["expired"]:
        warnings.append("The login in this file has already expired.")
    if merged.no_login:
        warnings.append("No login found for " + ", ".join(cookie_lib.site_label(s) for s in merged.no_login) + ".")
    return {"saved": [cookie_lib.site_label(s) for s in merged.added + merged.replaced],
            "kept": [cookie_lib.site_label(s) for s in merged.kept], "warnings": warnings}


def remove_cookie_site(user_id: int, site: str) -> bool:
    return cookie_lib.remove_site(cookie_path(user_id), site)


# ------------------------------------------------------------------ toolbox uploads
async def receive_upload(stream, user_id: int, name: str, rid: str, max_per_user: int = 3) -> tools.StoredInput:
    """Write the request body to the tool store (never past MAX_UPLOAD_BYTES) and look at it with ffprobe."""
    waiting = tools.store.for_user(user_id)
    for old in waiting[:max(0, len(waiting) - (max_per_user - 1))]:
        tools.store.discard(old)
    suffix = re.sub(r"[^a-z0-9.]", "", Path(name).suffix.lower())[:8] or ".bin"
    path = tools.store.folder(rid) / f"original{suffix}"
    written = 0
    try:
        with open(path, "wb") as fh:
            async for chunk in stream:
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise WebError("That file is bigger than the 2 GB limit.", 413)
                fh.write(chunk)
        if written == 0:
            raise WebError("The file is empty.")
        info = await tools.probe(path)
    except tools.ToolError as exc:
        tools.store.discard(rid)
        raise WebError(str(exc))
    except BaseException:
        tools.store.discard(rid)
        raise
    return tools.store.add(rid, user_id, path, name[:200] or "file", info)


def tool_view(rid: str, item) -> dict:
    info = item.info
    return {"rid": rid, "name": item.name, "duration": info.duration, "size": info.size, "width": info.width,
            "height": info.height, "has_video": info.has_video, "has_audio": info.has_audio,
            "burn_ok": bool(info.has_video and tools.burn_allowed(info.duration)),
            "has_srt": item.srt is not None,
            "compress_targets": [mb for mb in tools.COMPRESS_TARGETS_MB if mb < info.size / 1_000_000 * 0.9]}


def tool_params(item, tool: str, req: dict) -> dict:
    """Validate a toolbox request; the same parsers the bot uses for typed input."""
    info = item.info
    try:
        if tool == "trim":
            start, end = tools.parse_range(str(req.get("range", "")), info.duration)
            return {"start": start, "end": end, "exact": bool(req.get("exact")) and info.has_video}
        if tool == "audio":
            fmt = str(req.get("audio_format", "mp3"))
            if fmt not in tools.AUDIO_FORMATS or not info.has_audio:
                raise WebError("Choose MP3 or M4A.")
            return {"audio_format": fmt}
        if tool == "compress":
            target = req.get("target_mb")
            if target not in tools.COMPRESS_TARGETS_MB or not info.has_video:
                raise WebError("Choose one of the offered sizes.")
            tools.plan_compress(info.duration, target, info.height)
            return {"target_mb": target}
        if tool == "gif":
            if not info.has_video:
                raise WebError("A GIF needs a video.")
            start, length = tools.parse_gif(str(req.get("range", "")), info.duration)
            return {"start": start, "length": length}
        if tool == "strip":
            return {}
        if tool == "burn":
            if not info.has_video or not tools.burn_allowed(info.duration):
                raise WebError("That video is too long to burn subtitles into.")
            if item.srt is None:
                raise WebError("Add the subtitle (.srt) file first.")
            return {}
    except tools.ToolError as exc:
        raise WebError(str(exc))
    raise WebError("Unknown tool.")


def save_srt(user_id: int, rid: str, data: bytes) -> None:
    item = tools.store.get(rid, user_id)
    if item is None:
        raise WebError("That file expired - upload it again.", 404)
    if not data or len(data) > 2_000_000:
        raise WebError("That subtitle file is empty or too big.")
    path = tools.store.folder(rid) / "subs.srt"
    path.write_bytes(data)
    try:
        tools.normalize_srt(path)
    except tools.ToolError as exc:
        path.unlink(missing_ok=True)
        raise WebError(str(exc))
    item.srt = path
