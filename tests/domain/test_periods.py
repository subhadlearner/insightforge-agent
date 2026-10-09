from datetime import date

import pytest

from insightforge_agent.domain.periods import parse_period, period_end, periods_overlap


@pytest.mark.parametrize("text,expected", [
    ("2025", (date(2025, 1, 1), date(2025, 12, 31))),
    ("2025-Q3", (date(2025, 7, 1), date(2025, 9, 30))),
    ("2025-H1", (date(2025, 1, 1), date(2025, 6, 30))),
    ("2024-02", (date(2024, 2, 1), date(2024, 2, 29))),
    ("2025-09-14", (date(2025, 9, 14), date(2025, 9, 14))),
    ("2025-01..2025-06", (date(2025, 1, 1), date(2025, 6, 30))),
    ("2025-03 to 2025-04", (date(2025, 3, 1), date(2025, 4, 30))),
])
def test_periods_parse_to_first_and_last_day(text, expected):
    assert parse_period(text) == expected


@pytest.mark.parametrize("text", [None, "", "UNKNOWN", "unknown", "last spring", "2025-13",
                                  "2025-06..2025-01"])
def test_unreadable_periods_are_unknown(text):
    assert parse_period(text) is None
    assert period_end(text) is None


def test_overlap_is_inclusive_and_symmetric():
    assert periods_overlap("2025", "2025-Q3")
    assert periods_overlap("2025-Q1", "2025-03")
    assert not periods_overlap("2024", "2025")
    assert periods_overlap("2025-Q3", "2025") == periods_overlap("2025", "2025-Q3")


def test_an_unknown_period_never_overlaps_anything_not_even_itself():
    assert not periods_overlap("UNKNOWN", "2025")
    assert not periods_overlap("UNKNOWN", "UNKNOWN")
    assert not periods_overlap(None, None)
