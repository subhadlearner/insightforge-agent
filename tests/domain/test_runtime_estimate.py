"""T14 prerequisite (#15): the pre-submit runtime estimate and the source-toggle default rule
it shares with Run submission. Pure functions, no I/O."""

import pytest

from insightforge_agent.domain.contracts import Brief, effective_source_toggles
from insightforge_agent.domain.runtime_estimate import EstimateCoefficients, estimate_runtime

DEFAULTS = EstimateCoefficients(base_seconds=30, pdf_ingest_seconds=15,
                                source_seconds={"web": 45, "documents": 30, "memory": 15})


def test_the_handover_example_is_135_seconds():
    estimate = estimate_runtime(DEFAULTS, {"web": True, "documents": True, "memory": False}, 2)
    assert estimate.estimated_seconds == 30 + 2 * 15 + 45 + 30 == 135
    assert estimate.base_seconds == 30
    assert estimate.document_count == 2
    assert estimate.source_types == ["web", "documents"]


def test_only_the_base_when_nothing_is_enabled_and_no_documents():
    estimate = estimate_runtime(DEFAULTS, {"web": False}, 0)
    assert estimate.estimated_seconds == 30 and estimate.source_types == []


@pytest.mark.parametrize("source, seconds", [("web", 45), ("documents", 30), ("memory", 15)])
def test_each_enabled_source_type_adds_its_own_cost_once(source, seconds):
    assert estimate_runtime(DEFAULTS, {source: True}, 0).estimated_seconds == 30 + seconds


def test_source_types_are_listed_in_canonical_order_whatever_the_toggle_order():
    toggles = {"memory": True, "documents": True, "web": True}
    estimate = estimate_runtime(DEFAULTS, toggles, 0)
    assert estimate.source_types == ["web", "documents", "memory"]
    assert estimate.estimated_seconds == 30 + 45 + 30 + 15


def test_each_document_adds_the_ingest_cost():
    assert [estimate_runtime(DEFAULTS, {}, n).estimated_seconds for n in (0, 1, 10)] == [
        30, 45, 180]


def test_the_coefficients_decide_the_result():
    tuned = EstimateCoefficients(base_seconds=5, pdf_ingest_seconds=7,
                                 source_seconds={"web": 11, "documents": 13, "memory": 17})
    estimate = estimate_runtime(tuned, {"web": True, "memory": True}, 3)
    assert estimate.estimated_seconds == 5 + 3 * 7 + 11 + 17
    assert estimate.base_seconds == 5


def test_a_negative_document_count_is_refused():
    with pytest.raises(ValueError):
        estimate_runtime(DEFAULTS, {"web": True}, -1)


def test_estimates_are_deterministic():
    toggles = {"web": True, "documents": True}
    assert estimate_runtime(DEFAULTS, toggles, 4) == estimate_runtime(DEFAULTS, toggles, 4)


# --- the default rule shared with submission ------------------------------------------------

def test_explicit_toggles_win_over_the_persona():
    assert effective_source_toggles({"memory": True}, {"web": True}) == {"memory": True}


def test_explicit_empty_toggles_are_still_explicit():
    assert effective_source_toggles({}, {"web": True, "memory": True}) == {}


def test_omitted_toggles_inherit_the_persona():
    persona = {"web": True, "documents": False, "memory": True}
    assert effective_source_toggles(None, persona) == persona


def test_a_persona_without_toggles_means_web_only():
    assert effective_source_toggles(None, {}) == {"web": True}
    assert effective_source_toggles(None, {}) == Brief(text="x").source_toggles
