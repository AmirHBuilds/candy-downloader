"""
Candy-style progress bar, kept inside a <code> block so it renders as
fixed-width monospace on every device instead of drifting/wrapping
unevenly. Uses the owner's chosen emoji as the "filled" segment so the
whole bot feels personalized, not just the welcome text.

🍬 Downloading
<code>🍬🍬🍬🍬🍬🍬-------</code>  52%
⚡ 2.1MB/s · ⏳ 18s
"""
from config import OWNER_EMOJI

FILLED = OWNER_EMOJI
EMPTY = "-"
BAR_LENGTH = 14


def render_bar(percent: float) -> str:
    percent = max(0.0, min(100.0, percent))
    filled_count = round((percent / 100) * BAR_LENGTH)
    return FILLED * filled_count + EMPTY * (BAR_LENGTH - filled_count)


def render_status(stage_emoji: str, stage_text: str, percent: float | None,
                   speed: str | None = None, eta: str | None = None) -> str:
    lines = [f"{stage_emoji} <b>{stage_text}</b>"]
    if percent is not None:
        lines.append(f"<code>{render_bar(percent)}</code>  {percent:.0f}%")
    meta = []
    if speed:
        meta.append(speed)
    if eta and eta not in ("~", ""):
        meta.append(f"ETA {eta}")
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)
