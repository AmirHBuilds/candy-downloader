"""
Which tool(s) to try for a given domain, in priority order.

IMPORTANT: for domains we know are video/gallery sites, we do NOT append
the generic (aria2c) downloader as a fallback. If yt-dlp fails on a
youtube.com link, the right move is to surface that error (likely means
yt-dlp needs updating, or the video is private/region-locked) - NOT to
silently fetch the raw webpage HTML instead, which looks like a "success"
but produces garbage. The generic downloader is only appropriate for
domains we don't recognize at all, where the link might genuinely be a
plain direct file (a .pdf, .zip, .mp4 hosted directly, etc).
"""

# domain fragment -> ordered list of tool names to try, no generic fallback
OVERRIDES: dict[str, list[str]] = {
    "instagram.com": ["ytdlp", "gallerydl"],
    "pixiv.net": ["gallerydl"],
    "danbooru.donmai.us": ["gallerydl"],
    "gelbooru.com": ["gallerydl"],
    "deviantart.com": ["gallerydl"],
    "twitter.com": ["gallerydl", "ytdlp"],
    "x.com": ["gallerydl", "ytdlp"],
    "reddit.com": ["ytdlp", "gallerydl"],
    # Pinterest pins are usually images, not video - gallery-dl handles
    # that correctly and gives an honest "just download it" menu; yt-dlp
    # errors on image pins ("No video formats found") even though the
    # content itself is perfectly downloadable via gallery-dl.
    "pinterest.com": ["gallerydl", "ytdlp"],
    "pin.it": ["gallerydl", "ytdlp"],
    # Spotify streams are DRM-protected - handled entirely differently
    # (search + download matching audio elsewhere), see spotify_handler.py.
    "open.spotify.com": ["spotify"],
    "spotify.com": ["spotify"],
}

# Domains yt-dlp is known to handle well on its own - no generic fallback,
# a failure here should be reported, not papered over with a page-scrape.
KNOWN_STREAMING_DOMAINS = [
    "youtube.com", "youtu.be", "tiktok.com", "vimeo.com", "twitch.tv",
    "soundcloud.com", "facebook.com", "dailymotion.com", "bilibili.com",
    "streamable.com", "vk.com", "rumble.com",
    # Adult video sites yt-dlp explicitly supports - falling through to
    # gallery-dl/generic after yt-dlp gives a real error (403, geo-block,
    # etc.) here is pointless: gallery-dl doesn't support them and the
    # generic downloader just hangs trying to fetch what's actually a
    # webpage, not a file. Report yt-dlp's real error instead.
    "pornhub.com", "xvideos.com", "xhamster.com", "xnxx.com",
]


def _hostname(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domain: str) -> bool:
    """True if `host` IS `domain`, or a subdomain of it (m.youtube.com
    matches youtube.com). Deliberately NOT a substring check - that used
    to match "x.com" inside "dropbox.com"/"box.com"/"netflix.com", or
    any domain name appearing later in the URL (e.g. inside a redirect's
    query string), routing those links to the wrong tools entirely."""
    return host == domain or host.endswith("." + domain)


def tool_order_for(url: str) -> list[str]:
    host = _hostname(url)

    for domain, order in OVERRIDES.items():
        if _matches(host, domain):
            return order

    for domain in KNOWN_STREAMING_DOMAINS:
        if _matches(host, domain):
            return ["ytdlp"]

    # Truly unrecognized domain: could be a video site yt-dlp still knows
    # about (it supports 1800+ sites, more than we can enumerate), a
    # gallery, or a plain direct file link - try all three in order.
    return ["ytdlp", "gallerydl", "generic"]
