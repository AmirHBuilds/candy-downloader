"""
Time-range "sections": download only e.g. 1:50:00-1:52:00 of a long video.

Pure functions only - deliberately no telegram / yt_dlp imports, so this can
be unit-tested anywhere and imported from both the menu code (main.py) and
the downloader.

A section is a (start_seconds, end_seconds) tuple of floats. Once a section
has been through make_section() it is always CONCRETE: an empty Start became
0 and an empty End became the video's duration. Everything downstream
(the handler, filenames, /history) can therefore rely on two real numbers.

Every error raised here is a SectionError whose message is written for the
end user. It is PLAIN TEXT and can echo what the person typed, so callers
that put it in a parse_mode=HTML message must escape it (utils.text.esc).
"""
import re

MAX_SECTIONS = 10
MIN_SECTION_SECONDS = 1

_HELP = "Send a time like 1:50:00, 50:00, or a number of seconds like 90."

# People type timestamps in their own digits (Persian / Arabic-Indic), and
# phones sometimes insert a full-width colon.
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})
_DIGIT_MAP.update({ord("："): ":", ord("٫"): ".", ord("،"): "."})


class SectionError(ValueError):
    """A problem with user-supplied section input; the message is user-facing."""


def parse_timestamp(text: str) -> float:
    """'1:50:00', '50:00', '110:00', '90', '1:05.5' -> seconds.

    Accepted: H:MM:SS, MM:SS (minutes may exceed 59, so '110:00' works) and
    plain seconds. With colons, seconds (and minutes in the H:MM:SS form)
    must be below 60, so '1:75:00' is rejected rather than silently
    reinterpreted."""
    cleaned = (text or "").translate(_DIGIT_MAP).strip()
    if not cleaned:
        raise SectionError(_HELP)

    parts = cleaned.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+", p) for p in parts[:-1]) \
            or not re.fullmatch(r"\d+(\.\d+)?", parts[-1]):
        raise SectionError(f"“{text.strip()[:30]}” isn't a valid time. {_HELP}")

    numbers = [float(p) for p in parts]
    if len(numbers) == 1:
        return numbers[0]
    if numbers[-1] >= 60:
        raise SectionError("Seconds must be below 60 (for example 1:05, not 1:75).")
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    hours, minutes, seconds = numbers
    if minutes >= 60:
        raise SectionError("Minutes must be below 60 when hours are given (for example 1:05:00).")
    return hours * 3600 + minutes * 60 + seconds


def format_timestamp(seconds: float) -> str:
    """6600 -> '1:50:00', 307 -> '5:07', 90.5 -> '1:30.5'."""
    tenths = round(max(0.0, float(seconds)) * 10)   # rounding first avoids '59.10'
    whole, tenth = divmod(tenths, 10)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    tail = f"{secs:02d}" + (f".{tenth}" if tenth else "")
    return f"{hours}:{minutes:02d}:{tail}" if hours else f"{minutes}:{tail}"


def format_section(start: float, end: float) -> str:
    """(6600, 6720) -> '1:50:00 – 1:52:00'."""
    return f"{format_timestamp(start)} – {format_timestamp(end)}"


def section_length(start: float, end: float) -> float:
    return end - start


def filename_tag(start: float, end: float) -> str:
    """Filename-safe range for clip names: '01-50-00–01-52-00'. Whole seconds
    (a clip named to the tenth of a second would just be noise)."""
    def one(value: float) -> str:
        whole = int(round(value))
        return f"{whole // 3600:02d}-{whole % 3600 // 60:02d}-{whole % 60:02d}"
    return f"{one(start)}–{one(end)}"


def make_section(start: float | None, end: float | None, duration: float | None) -> tuple[float, float]:
    """Validate one section (what the Save button runs) and make it concrete.

    start/end are seconds, or None for 'left empty'. Both empty is an error -
    that would just be the whole video. duration None means 'unknown': the
    upper-bound checks are skipped (used when re-checking at job time)."""
    if start is None and end is None:
        raise SectionError("Set a start time, an end time, or both. They can't both be empty.")

    try:
        real_start = 0.0 if start is None else float(start)
        if end is None:
            if duration is None:
                raise SectionError("Set an end time - the video length isn't known.")
            real_end = float(duration)
        else:
            real_end = float(end)
    except (TypeError, ValueError):
        raise SectionError("Something is wrong with a saved section - please add it again.")

    if duration is not None:
        if real_start >= duration:
            raise SectionError(
                f"Start {format_timestamp(real_start)} is past the end of the video "
                f"(it's {format_timestamp(duration)} long)."
            )
        if real_end > duration:
            raise SectionError(
                f"End {format_timestamp(real_end)} is past the end of the video "
                f"(it's {format_timestamp(duration)} long)."
            )
    if real_end <= real_start:
        raise SectionError("The end has to be after the start.")
    if real_end - real_start < MIN_SECTION_SECONDS:
        raise SectionError("A section has to be at least 1 second long.")
    return real_start, real_end


def check_can_add(current_count: int) -> None:
    if current_count >= MAX_SECTIONS:
        raise SectionError(f"You can add up to {MAX_SECTIONS} sections per download.")


def validate_sections(sections, duration: float | None = None) -> list[tuple[float, float]]:
    """Final check of the whole list when a job starts. Keeps the order the
    person added them in (that's the order clips are sent / merged), drops
    exact duplicates, and re-runs make_section on each entry. Overlapping
    sections are allowed."""
    if not sections:
        raise SectionError("Add at least one section first.")
    result: list[tuple[float, float]] = []
    for item in sections:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise SectionError("Something is wrong with a saved section - please add it again.")
        start, end = item
        section = make_section(start, end, duration)
        if section not in result:
            result.append(section)
    if len(result) > MAX_SECTIONS:
        raise SectionError(f"You can add up to {MAX_SECTIONS} sections per download.")
    return result
