"""
Best-effort fallback probe using gallery-dl's own metadata dump. Used
only when yt-dlp's probe fails outright (see downloader/probe.py) - most
often on Instagram, where yt-dlp's extractor is more easily blocked than
gallery-dl's. gallery-dl's JSON structure varies by extractor, so this
is deliberately defensive: any parse failure just means no preview,
not a crash.
"""
import asyncio
import json
import logging

log = logging.getLogger("candy.gdlprobe")


async def probe(url: str) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gallery-dl", "-j", "--no-download", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        data = json.loads(out.decode(errors="ignore"))

        for entry in data:
            if not (isinstance(entry, list) and len(entry) >= 3 and isinstance(entry[-1], dict)):
                continue
            meta = entry[-1]
            title = (meta.get("description") or meta.get("title") or meta.get("content") or "").strip()
            thumb = entry[1] if isinstance(entry[1], str) and entry[1].startswith("http") else ""
            if title or thumb:
                return {"title": title[:150], "thumbnail": thumb}
    except Exception as exc:  # noqa: BLE001
        log.info("gallery-dl fallback probe failed for %s: %s", url, exc)
    return None
