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
