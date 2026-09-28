"""
Progress bar rendering. Five styles, chosen per user in /settings.

Four of the five ("candy", "jar", "pacman", "slider") fill using
config.OWNER_EMOJI, not a hardcoded 🍬 - this bot is templated per owner
(OWNER_NAME/OWNER_EMOJI in .env), and a bar that only ever drew candy
regardless of that config would be a lie for anyone who isn't literally
running "Candy". Their display names follow OWNER_NAME the same way
(BAR_STYLES is built once at import time): "Candy" / "Candy Jar" /
"Sliding candy" become "<name>" / "<name> Jar" / "Sliding <name>". Pac-Man
and the moon are genuinely their own thing and stay as they are.

  candy   🍬🍬🍬🍬◾️◾️◾️◾️◾️◾️     10 slots, each = 10%  (default)
  jar     🫙🍬🍬🍬🍬······        a jar filling up
  pacman  ・・・・😋🍬🍬🍬🍬🍬     eats its way through
  slider  ····🍬·····            one piece sliding along a track
  moon    🌕🌕🌓🌑🌑             five moons, each waxing through 4 phases

"auto" (the default setting) means moon in ADHD Mode, "candy" otherwise.
"""
from config import OWNER_EMOJI, OWNER_NAME

SLOTS = 10
EMPTY_SLOT = "◾️"   # unfilled slot - a neutral glyph, deliberately not owner-branded
_MOON_PHASES = ["🌑", "🌒", "🌓", "🌔", "🌕"]


def _clamp(percent: float) -> float:
    return max(0.0, min(100.0, percent))


def _candy(percent: float) -> str:
    filled = int(_clamp(percent) // (100 / SLOTS))
    return OWNER_EMOJI * filled + EMPTY_SLOT * (SLOTS - filled)


def _jar(percent: float) -> str:
    filled = int(_clamp(percent) // (100 / SLOTS))
    return "🫙" + OWNER_EMOJI * filled + "·" * (SLOTS - filled)


def _pacman(percent: float) -> str:
    eaten = int(_clamp(percent) // (100 / SLOTS))
    if eaten >= SLOTS:
        return "・" * SLOTS + "😋"
    return "・" * eaten + "😋" + OWNER_EMOJI * (SLOTS - eaten - 1)


def _slider(percent: float) -> str:
    pos = min(SLOTS - 1, int(_clamp(percent) // (100 / SLOTS)))
    return "·" * pos + OWNER_EMOJI + "·" * (SLOTS - 1 - pos)


def _moon(percent: float) -> str:
    # 5 moons x 4 steps each = 20 steps, so it advances every 5%.
    units = int(_clamp(percent) // 5)
    moons = []
    for i in range(5):
        moons.append(_MOON_PHASES[max(0, min(4, units - i * 4))])
    return "".join(moons)


BAR_STYLES = {
    "candy": (OWNER_NAME, _candy),
    "jar": (f"{OWNER_NAME} Jar", _jar),
    "pacman": ("Pac-Man", _pacman),
    "slider": (f"Sliding {OWNER_NAME}", _slider),
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
