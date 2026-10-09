"""As-of periods as text ("2025", "2025-Q3", "2025-09", "2025-09-14", "2025-01..2025-06").

Pure parsing and comparison. A period that cannot be read is UNKNOWN, and UNKNOWN never
overlaps anything: an unknown period is never assumed to match another one."""

import calendar
import re
from datetime import date

UNKNOWN = "UNKNOWN"

_POINT = re.compile(r"^(\d{4})(?:-(?:(Q[1-4])|(H[12])|(\d{2})(?:-(\d{2}))?))?$", re.IGNORECASE)
_RANGE_SPLIT = re.compile(r"\s*(?:\.\.|/|\bto\b)\s*", re.IGNORECASE)


def _point(text: str) -> tuple[date, date] | None:
    m = _POINT.match(text.strip())
    if m is None:
        return None
    year, quarter, half, month, day = m.groups()
    y = int(year)
    try:
        if quarter:
            q = int(quarter[1])
            return date(y, 3 * q - 2, 1), date(y, 3 * q, calendar.monthrange(y, 3 * q)[1])
        if half:
            return (date(y, 1, 1), date(y, 6, 30)) if half[1] == "1" else (date(y, 7, 1), date(y, 12, 31))
        if month and day:
            d = date(y, int(month), int(day))
            return d, d
        if month:
            mo = int(month)
            return date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
        return date(y, 1, 1), date(y, 12, 31)
    except ValueError:
        return None


def parse_period(text: str | None) -> tuple[date, date] | None:
    """The first and last day a period covers, or None when it is UNKNOWN or unreadable."""
    if not text or text.strip().upper() == UNKNOWN:
        return None
    parts = _RANGE_SPLIT.split(text.strip())
    if len(parts) == 1:
        return _point(parts[0])
    if len(parts) == 2:
        start, end = _point(parts[0]), _point(parts[1])
        if start and end and start[0] <= end[1]:
            return start[0], end[1]
    return None


def periods_overlap(a: str | None, b: str | None) -> bool:
    pa, pb = parse_period(a), parse_period(b)
    if pa is None or pb is None:
        return False
    return pa[0] <= pb[1] and pb[0] <= pa[1]


def period_end(text: str | None) -> date | None:
    parsed = parse_period(text)
    return parsed[1] if parsed else None


def stated_period(text: str | None) -> str:
    """The period as given when it can be read, else UNKNOWN."""
    return text.strip() if text and parse_period(text) else UNKNOWN


_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
_QUARTER_WORDS = ["first", "second", "third", "fourth"]


def period_stated_in(period: str | None, text: str) -> bool:
    """Whether the text actually states the period: the year(s) appear in it, and so does any
    finer part (quarter, half or month). A well-formed period the text does not state is not
    evidence of anything."""
    if not period or parse_period(period) is None:
        return False
    low = text.casefold()
    for point in _RANGE_SPLIT.split(period.strip()):
        m = _POINT.match(point.strip())
        if m is None:
            return False
        year, quarter, half, month, day = m.groups()
        if year not in text:
            return False
        if quarter:
            n = int(quarter[1])
            if not any(t in low for t in (f"q{n}", f"{n}q", f"{_QUARTER_WORDS[n - 1]} quarter")):
                return False
        if half:
            n = int(half[1])
            if not any(t in low for t in (f"h{n}", f"{n}h", f"{('first', 'second')[n - 1]} half")):
                return False
        if month and not _month_stated(low, year, int(month), int(day) if day else None):
            return False
    return True


def _month_stated(low: str, year: str, month: int, day: int | None) -> bool:
    """The month, and for a full date the day next to it ("September 14", "14th of September",
    or the ISO date itself). A bare number elsewhere in the text is not a day."""
    name = _MONTHS[month - 1]
    abbr = name[:3] + ("t?" if name == "september" else "")
    mon = rf"(?:{name}|{abbr}\.?)"
    if day is None:
        return f"{year}-{month:02d}" in low or re.search(rf"\b{mon}\b", low) is not None
    if f"{year}-{month:02d}-{day:02d}" in low:
        return True
    return re.search(
        rf"\b{mon}\s+0?{day}(?!\d)|(?<!\d)0?{day}(?:st|nd|rd|th)?\s+(?:of\s+)?{mon}\b", low
    ) is not None
