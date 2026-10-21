import pytest

from insightforge_agent.domain.periods import period_stated_in


@pytest.mark.parametrize("period,text,expected", [
    ("2025", "sold 4.27 million in 2025", True),
    ("2025", "sold 4.27 million in 2019", False),
    ("2025-Q3", "third quarter of 2025", True),
    ("2025-Q3", "sales in 2025", False),
    ("2025-H1", "H1 2025 deliveries", True),
    ("2025-09", "In September 2025", True),
    ("2025-09", "Sep 2025", True),
    ("2025-09", "in 2025 overall", False),
    ("2025-01..2025-06", "from January 2025 to June 2025", True),
    ("UNKNOWN", "2025", False),
])
def test_a_period_counts_as_stated_only_when_the_text_states_it(period, text, expected):
    assert period_stated_in(period, text) is expected


@pytest.mark.parametrize("period,text,expected", [
    ("2025-09-14", "Sales were reported in September 2025", False),      # day not stated
    ("2025-09-14", "On September 14, 2025 BYD said", True),
    ("2025-09-14", "on 14 September 2025", True),
    ("2025-09-14", "the 14th of September, 2025", True),
    ("2025-09-14", "published 2025-09-14", True),
    ("2025-09-14", "sold 14 cars in September 2025", False),             # a bare 14 is not a day
    ("2025-09-05", "Sept. 5, 2025", True),
    ("2025-09-05", "September 15, 2025", False),
    ("2025-09-14..2025-09-20", "from September 14 to September 20, 2025", True),
    ("2025-09-14..2025-09-20", "from September 14 to September 21, 2025", False),
])
def test_a_full_date_needs_its_day_stated_next_to_its_month(period, text, expected):
    assert period_stated_in(period, text) is expected
