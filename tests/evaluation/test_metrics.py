"""Deterministic metrics over a stored Report (T16 #17).

Fact-check pass rate: the Fact-Checker's own persisted counts, `unchecked` left out
(CONTEXT.md, Claim verdicts), pooled over Claims across Reports. Citation accuracy: every Claim,
sampled or not, resolved along Claim -> item -> primary Passage -> Source, and accurate only
when that Passage states every figure of the Claim."""

import pytest

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.evaluation.metrics import (
    MetricError,
    citation_metrics,
    fact_check_metrics,
    pool_citations,
    pool_fact_checks,
)
from tests.evaluation.builders import (
    OWNER,
    World,
    claim,
    historical,
    item,
    report,
    section,
    verdict,
)

# --- fact-check pass rate --------------------------------------------------------------------


def checked_report(*outcomes: str):
    w = World()
    refs = w.web("https://a.example/1", *[f"Paragraph {n}." for n in range(len(outcomes))])
    items = [item(f"ev_{n}", refs[n]) for n in range(len(outcomes))]
    claims = [claim(f"Claim {n}.", f"ev_{n}") for n in range(len(outcomes))]
    verdicts = [verdict(0, n, f"ev_{n}", o, refs[n]) for n, o in enumerate(outcomes)]
    return report([section("Findings", *claims)], items, verdicts=verdicts)


def test_unchecked_claims_are_left_out_of_the_pass_rate():
    m = fact_check_metrics(checked_report("verified", "verified", "verified", "unverified",
                                          "unchecked", "unchecked"))
    assert (m.verified, m.unverified, m.unchecked, m.sampled) == (3, 1, 2, 6)
    assert m.pass_rate == 0.75  # 3 of the 4 checked, not 3 of the 6 sampled


def test_a_report_with_nothing_checkable_has_no_pass_rate_never_zero():
    m = fact_check_metrics(checked_report("unchecked", "unchecked"))
    assert m.pass_rate is None and m.checked == 0


def test_a_summary_that_disagrees_with_its_verdicts_is_refused():
    r = checked_report("verified", "unverified")
    r.body["verified"]["summary"]["verified"] = 2
    with pytest.raises(MetricError, match="verdicts"):
        fact_check_metrics(r)


def test_a_report_without_a_fact_check_summary_is_refused():
    r = checked_report("verified")
    del r.body["verified"]
    with pytest.raises(MetricError, match="fact-check"):
        fact_check_metrics(r)


def test_pooling_weights_every_checked_claim_equally_not_every_report():
    small = fact_check_metrics(checked_report("verified"))
    large = fact_check_metrics(checked_report(*["verified"] * 3 + ["unverified"] * 3))
    pooled = pool_fact_checks([small, large])
    assert (pooled.verified, pooled.checked) == (4, 7)
    assert pooled.pass_rate == pytest.approx(4 / 7)  # the mean of 1.0 and 0.5 would be 0.75


def test_pooling_nothing_measurable_has_no_pass_rate():
    assert pool_fact_checks([]).pass_rate is None
    assert pool_fact_checks([fact_check_metrics(checked_report("unchecked"))]).pass_rate is None


# --- citation accuracy -----------------------------------------------------------------------


def test_a_claim_whose_figures_are_in_its_primary_passage_is_accurate():
    w = World()
    [p] = w.web("https://a.example/1", "BYD sold 4.27 million vehicles in 2025.")
    r = report([section("Sales", claim("BYD sold 4.27 million vehicles in 2025.", "ev_1"))],
               [item("ev_1", p)])
    m = citation_metrics(w.store, OWNER, r)
    assert (m.claims, m.accurate, m.unresolved, m.figures_not_in_passage) == (1, 1, 0, 0)
    assert m.accuracy == 1.0 and m.by_kind["web"].accurate == 1


def test_a_figure_only_a_non_primary_passage_states_is_not_an_accurate_citation():
    """The citation shown is the primary Passage; support elsewhere does not make it right."""
    w = World()
    primary, other = w.web("https://a.example/1", "BYD sold vehicles in 2025.",
                           "BYD sold 4.27 million vehicles in 2025.")
    r = report([section("Sales", claim("BYD sold 4.27 million vehicles in 2025.", "ev_1"))],
               [item("ev_1", primary, other)])
    m = citation_metrics(w.store, OWNER, r)
    assert (m.accurate, m.figures_not_in_passage) == (0, 1)
    assert "4.27" in m.problems[0]


def test_an_unresolvable_claim_stays_in_the_denominator():
    w = World()
    [p] = w.web("https://a.example/1", "Revenue was 612 million dollars.")
    r = report([section("Revenue", claim("Revenue was 612 million dollars.", "ev_1"),
                        claim("Margins rose.", "ev_missing"))], [item("ev_1", p)])
    m = citation_metrics(w.store, OWNER, r)
    assert (m.claims, m.accurate, m.unresolved) == (2, 1, 1)
    assert m.accuracy == 0.5  # never 1 of the 1 that resolved


def test_a_passage_missing_from_the_store_is_unresolved():
    w = World()
    gone = PassageRef(observation_id="obs_gone", index=0)
    r = report([section("Revenue", claim("Margins rose.", "ev_1"))], [item("ev_1", gone)])
    m = citation_metrics(w.store, OWNER, r)
    assert (m.unresolved, m.accuracy) == (1, 0.0)


def test_another_owners_passage_is_unresolved():
    w = World()
    [p] = w.web("https://a.example/1", "Margins rose.")
    r = report([section("Revenue", claim("Margins rose.", "ev_1"))], [item("ev_1", p)])
    assert citation_metrics(w.store, "bob", r).unresolved == 1


def test_a_historical_claims_report_date_is_not_a_figure_and_it_cites_the_original_passage():
    w = World()
    [orig] = w.web("https://old.example/1", "BYD sold 4.27 million vehicles in 2025.")
    hist = historical("hist_1", orig)
    text = "BYD sold 4.27 million vehicles in 2025 (reported 2026-03-01)."
    r = report([section("Earlier findings", claim(text, "hist_1", historical_=True))],
               background=[hist])
    m = citation_metrics(w.store, OWNER, r)
    assert (m.accurate, m.by_kind["historical"].accurate) == (1, 1)


def test_a_historical_claim_never_resolves_through_a_current_evidence_item():
    """`historical` picks the Historical context; the same id in the bundle does not count."""
    w = World()
    [p] = w.web("https://a.example/1", "Margins rose.")
    r = report([section("Earlier findings", claim("Margins rose.", "ev_1", historical_=True))],
               [item("ev_1", p)])
    assert citation_metrics(w.store, OWNER, r).unresolved == 1


def test_document_claims_are_counted_as_documents():
    w = World()
    [chunk] = w.pdf("annual.pdf", "Revenue reached 612 million dollars in fiscal 2025.")
    r = report([section("Revenue", claim("Revenue reached 612 million dollars in fiscal 2025.",
                                         "ev_1"))], [item("ev_1", chunk)])
    m = citation_metrics(w.store, OWNER, r)
    assert m.by_kind["document"].accurate == 1 and "web" not in m.by_kind


def test_the_unverified_mark_does_not_change_citation_accuracy():
    w = World()
    [p] = w.web("https://a.example/1", "Revenue was 612 million dollars.")
    r = report([section("Revenue", claim("Revenue was 612 million dollars.", "ev_1"))],
               [item("ev_1", p)], verdicts=[verdict(0, 0, "ev_1", "unverified", p)])
    assert citation_metrics(w.store, OWNER, r).accurate == 1


def test_a_report_with_no_claims_has_no_accuracy():
    w = World()
    m = citation_metrics(w.store, OWNER, report([section("Findings")]))
    assert m.claims == 0 and m.accuracy is None


def test_a_malformed_report_body_is_refused():
    w = World()
    r = report([section("Findings")])
    del r.body["evidence"]
    with pytest.raises(MetricError):
        citation_metrics(w.store, OWNER, r)


def test_pooled_citation_accuracy_counts_claims_across_reports():
    w = World()
    [p] = w.web("https://a.example/1", "Revenue was 612 million dollars.")
    good = citation_metrics(w.store, OWNER, report(
        [section("R", claim("Revenue was 612 million dollars.", "ev_1"))], [item("ev_1", p)]))
    bad = citation_metrics(w.store, OWNER, report(
        [section("R", *[claim("Revenue was 9 million dollars.", "ev_1")] * 3)],
        [item("ev_1", p)]))
    pooled = pool_citations([good, bad])
    assert (pooled.claims, pooled.accurate) == (4, 1) and pooled.accuracy == 0.25
    assert pool_citations([]).accuracy is None
