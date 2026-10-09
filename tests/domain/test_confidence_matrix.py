"""The current Source-score and Confidence matrix, documented as tests.

These pin today's behaviour (design.md section 7); they are not a statement that the scores are
well calibrated. Calibration (publication dates, byline, domain classes) belongs to the later
credibility work."""

from datetime import UTC, datetime
from itertools import product

import pytest

from insightforge_agent.agents.web import credibility_score
from insightforge_agent.domain.evidence import Thresholds, confidence_of, decision_rule

NOW = datetime(2026, 10, 9, tzinfo=UTC)
HIGH = Thresholds().credibility_high  # 0.65
AGES = {"undated": None, "fresh": datetime(2026, 5, 1, tzinfo=UTC),
        "two_years": datetime(2024, 10, 1, tzinfo=UTC), "old": datetime(2019, 1, 1, tzinfo=UTC)}

#           domain                       undated  fresh   2 years   old     (score, single-Source Confidence)
MATRIX = {
    "https://example.com/a":        dict(undated=(0.6, "LOW"), fresh=(0.7, "MEDIUM"),
                                         two_years=(0.6, "LOW"), old=(0.5, "LOW")),
    "https://www.reuters.com/a":    dict(undated=(0.6, "LOW"), fresh=(0.7, "MEDIUM"),
                                         two_years=(0.6, "LOW"), old=(0.5, "LOW")),
    "https://energy.example.gov/a": dict(undated=(0.8, "MEDIUM"), fresh=(0.9, "MEDIUM"),
                                         two_years=(0.8, "MEDIUM"), old=(0.7, "MEDIUM")),
    "https://uni.example.edu/a":    dict(undated=(0.8, "MEDIUM"), fresh=(0.9, "MEDIUM"),
                                         two_years=(0.8, "MEDIUM"), old=(0.7, "MEDIUM")),
    "https://body.example.int/a":   dict(undated=(0.8, "MEDIUM"), fresh=(0.9, "MEDIUM"),
                                         two_years=(0.8, "MEDIUM"), old=(0.7, "MEDIUM")),
}


@pytest.mark.parametrize("url,age", list(product(MATRIX, AGES)))
def test_single_source_score_and_confidence(url, age):
    score, confidence = MATRIX[url][age]
    assert credibility_score(url, AGES[age], NOW) == pytest.approx(score)
    assert confidence_of(has_independent_pair=False, best_credibility=score, conflicting=False,
                         high=HIGH) == confidence


def test_an_undated_ordinary_domain_cannot_reach_medium_with_the_current_wiring():
    """Publication dates are not extracted yet, so only trusted-suffix Sources reach MEDIUM on
    their own. Deliberately conservative: it yields under-confidence, never false confidence."""
    assert credibility_score("https://example.com/a", None, NOW) < HIGH


@pytest.mark.parametrize("pair,best,conflicting,expected,rule", [
    (True, 0.1, False, "HIGH", "independent_sources"),
    (True, 0.9, False, "HIGH", "independent_sources"),
    (False, 0.9, False, "MEDIUM", "high_credibility_single_source"),
    (False, 0.65, False, "MEDIUM", "high_credibility_single_source"),
    (False, 0.64, False, "LOW", "insufficient_credibility"),
    (True, 0.9, True, "LOW", "conflicting"),
    (False, 0.9, True, "LOW", "conflicting"),
])
def test_confidence_and_its_decision_rule_agree(pair, best, conflicting, expected, rule):
    kw = dict(has_independent_pair=pair, best_credibility=best, conflicting=conflicting, high=0.65)
    assert confidence_of(**kw) == expected
    assert decision_rule(**kw) == rule
