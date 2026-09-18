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
]


def tool_order_for(url: str) -> list[str]:
    for domain, order in OVERRIDES.items():
        if domain in url:
            return order

    for domain in KNOWN_STREAMING_DOMAINS:
        if domain in url:
            return ["ytdlp"]

    # Truly unrecognized domain: could be a video site yt-dlp still knows
    # about (it supports 1800+ sites, more than we can enumerate), a
    # gallery, or a plain direct file link - try all three in order.
    return ["ytdlp", "gallerydl", "generic"]
