"""Regression tests for `sources_independent`, extracted unchanged from T5 `synthesize` (M1.3).
Expected values follow the rule as it stood inline: different domains, and neither Source's text
links to the other by address or domain."""

import pytest

from insightforge_agent.domain.evidence import sources_independent

A, B = "https://www.a.example/report", "https://b.test/news"


@pytest.mark.parametrize("a, a_texts, b, b_texts, expected", [
    (A, ["BYD sold 4.27 million."], B, ["BYD sold 4.01 million."], True),
    # www. and case do not make a different domain.
    ("https://WWW.a.example/x", [], "https://a.example/y", [], False),
    # Same domain, different pages.
    ("https://a.example/1", [], "https://a.example/2", [], False),
    # One Source links to the other by full address, or by domain alone.
    (A, ["see https://b.test/news for details"], B, [], False),
    (A, ["per b.test"], B, [], False),
    (A, [], B, ["Source: A.EXAMPLE"], False),
    # A text that mentions neither is independent.
    (A, ["no links here"], B, ["nor here"], True),
    # No text at all: nothing cites anything.
    (A, [], B, [], True),
])
def test_sources_independent_is_unchanged(a, a_texts, b, b_texts, expected):
    assert sources_independent(a, a_texts, b, b_texts) is expected
    assert sources_independent(b, b_texts, a, a_texts) is expected  # symmetric
