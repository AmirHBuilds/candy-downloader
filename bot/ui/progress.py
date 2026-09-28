"""
Progress bar rendering. Five styles, chosen per user in /settings:

  candy   🍬🍬🍬🍬◾️◾️◾️◾️◾️◾️     10 slots, each candy = 10%  (default)
  jar     🫙🍬🍬🍬🍬······        a candy jar filling up
  pacman  ・・・・😋🍬🍬🍬🍬🍬     eats its way through the candies
  slider  ····🍬·····            one candy sliding along a track
  moon    🌕🌕🌓🌑🌑             five moons, each waxing through 4 phases

"auto" (the default setting) means moon in ADHD Mode and candy otherwise.
"""

SLOTS = 10
EMPTY_SLOT = "◾️"   # candy bar's unfilled slot (black square + emoji variation selector)
_MOON_PHASES = ["🌑", "🌒", "🌓", "🌔", "🌕"]


def _clamp(percent: float) -> float:
    return max(0.0, min(100.0, percent))


def _candy(percent: float) -> str:
    filled = int(_clamp(percent) // (100 / SLOTS))
    return "🍬" * filled + EMPTY_SLOT * (SLOTS - filled)


def _jar(percent: float) -> str:
    filled = int(_clamp(percent) // (100 / SLOTS))
    return "🫙" + "🍬" * filled + "·" * (SLOTS - filled)


def _pacman(percent: float) -> str:
    eaten = int(_clamp(percent) // (100 / SLOTS))
    if eaten >= SLOTS:
        return "・" * SLOTS + "😋"
    return "・" * eaten + "😋" + "🍬" * (SLOTS - eaten - 1)


def _slider(percent: float) -> str:
    pos = min(SLOTS - 1, int(_clamp(percent) // (100 / SLOTS)))
    return "·" * pos + "🍬" + "·" * (SLOTS - 1 - pos)


def _moon(percent: float) -> str:
    # 5 moons x 4 steps each = 20 steps, so it advances every 5%.
    units = int(_clamp(percent) // 5)
    moons = []
    for i in range(5):
        moons.append(_MOON_PHASES[max(0, min(4, units - i * 4))])
    return "".join(moons)


BAR_STYLES = {
    "candy": ("Candy", _candy),
    "jar": ("Candy jar", _jar),
    "pacman": ("Pac-Man", _pacman),
    "slider": ("Sliding candy", _slider),
    "moon": ("Moons", _moon),
}


def resolve_style(setting: str | None, adhd: bool) -> str:
    """Turn the stored setting ("auto" or a style name) into a real style."""
    if setting in BAR_STYLES:
        return setting
    return "moon" if adhd else "candy"


def render_progress(style: str, percent: float, speed: str | None = None,
                    label: str | None = None) -> str:
    """'Video - 42% • 🍬🍬🍬🍬◾️◾️◾️◾️◾️◾️ • 2.1MB/s'.

    label: which stream this is ("Video" / "Audio") - yt-dlp downloads them
    one after the other, each running 0-100%, so without a title the bar
    looks like it inexplicably restarted.

    Once a stream is complete the bar (and speed) are dropped and only the
    title and 100% remain: 'Video - 100%'."""
    pct = _clamp(percent)
    prefix = f"{label} - " if label else ""
    if round(pct) >= 100:
        return f"{prefix}100%"
    bar = BAR_STYLES.get(style, BAR_STYLES["candy"])[1](pct)
    parts = [f"{pct:.0f}%", bar]
    if speed:
        parts.append(speed)
    return prefix + " • ".join(parts)
