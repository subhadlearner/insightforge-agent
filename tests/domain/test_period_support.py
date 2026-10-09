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
