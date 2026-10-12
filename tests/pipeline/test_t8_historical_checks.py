"""T8 (#9): the Writer's rules for Historical Claims and the Fact-Checker's inherited checks.

A Historical Claim cites a Historical item, never an Evidence item; it shows its original Report
date inline; its figures and names are held to the earlier statement. A sampled one is checked
against its original Sources: re-fetched if a web page, the stored chunks if an uploaded
document, through the same F2 gates and entailment call as a current Claim."""

from datetime import date

from insightforge_agent.agents.web import FetchError
from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportDraft,
    ReportSection,
)
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.write import render_historical, violations
from tests.pipeline.test_t6_writer_fact_check import SALES, URL, Fetcher, draft_of, evidence

DAY = date(2026, 9, 1)
DATED = "BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024 (reported 2026-09-01)."


def background(primary: PassageRef, statement: str = SALES) -> HistoricalContext:
    return HistoricalContext(items=[HistoricalItem(
        id="hist_1", subtask_id="m1", aspect_id="a1", heading="Sales", statement=statement,
        entity="BYD", report_id="rep_old", run_id="run_old", report_date=DAY,
        as_of_period="2025", primary=primary, supporting=[primary])])


def historical_draft(text: str = DATED, evidence_id: str = "hist_1",
                     historical: bool = True) -> ReportDraft:
    return ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=text, evidence_id=evidence_id, historical=historical)])])


# --- Writer -----------------------------------------------------------------------------------

def test_a_historical_claim_with_its_date_inline_is_accepted(make_deps):
    deps = make_deps()
    obs, bundle = evidence(deps, [SALES])
    hist = background(PassageRef(observation_id=obs.id, index=0))
    assert violations(historical_draft(), bundle, hist) == []
    leading = "As reported on 2026-09-01, BYD sold 4.27 million vehicles in 2025."
    assert violations(historical_draft(leading), bundle, hist) == []


def test_a_historical_claim_must_show_its_original_report_date(make_deps):
    deps = make_deps()
    obs, bundle = evidence(deps, [SALES])
    hist = background(PassageRef(observation_id=obs.id, index=0))
    (problem,) = violations(historical_draft(SALES), bundle, hist)
    assert "does not show its report date inline as (reported 2026-09-01)" in problem
    as_of_only = "In 2025 BYD sold 4.27 million vehicles (as of 2025)."
    assert any("report date" in p for p in violations(historical_draft(as_of_only), bundle, hist))


def test_a_historical_claim_is_held_to_the_earlier_statement(make_deps):
    deps = make_deps()
    obs, bundle = evidence(deps, [SALES])
    hist = background(PassageRef(observation_id=obs.id, index=0))
    wrong = "BYD sold 4.9 million vehicles in 2025 (reported 2026-09-01)."
    named = "Tesla sold 4.27 million vehicles in 2025 (reported 2026-09-01)."
    assert any("['4.9'] not in historical item hist_1" in p
               for p in violations(historical_draft(wrong), bundle, hist))
    assert any("['Tesla'] not in historical item hist_1" in p
               for p in violations(historical_draft(named), bundle, hist))


def test_current_evidence_can_never_be_marked_historical(make_deps):
    deps = make_deps()
    obs, bundle = evidence(deps, [SALES])
    hist = background(PassageRef(observation_id=obs.id, index=0))
    (problem,) = violations(historical_draft(DATED, "ev_0"), bundle, hist)
    assert "is marked historical but 'ev_0' is current evidence" in problem
    (unknown,) = violations(historical_draft(DATED, "hist_nope"), bundle, hist)
    assert "not a historical item" in unknown


def test_a_historical_item_can_never_be_cited_as_current_evidence(make_deps):
    deps = make_deps()
    obs, bundle = evidence(deps, [SALES])
    hist = background(PassageRef(observation_id=obs.id, index=0))
    (problem,) = violations(historical_draft(SALES, historical=False), bundle, hist)
    assert "cites historical item 'hist_1' as current evidence" in problem


def test_the_writer_sees_background_apart_from_the_bundle_with_both_dates():
    hist = background(PassageRef(observation_id="obs_x", index=0))
    assert render_historical(hist) == (
        f"- [hist_1] (Sales; reported 2026-09-01; as of 2025) {SALES}")


# --- Fact-Checker: inherited provenance -----------------------------------------------------------

def check(deps, hist, text=DATED):
    (v,) = fact_check(deps, "alice", "run_new", historical_draft(text), EvidenceBundle(sections=[]),
                      hist).verdicts
    return v


def test_a_sampled_historical_web_claim_is_verified_against_its_original_page(make_deps):
    fetcher = Fetcher({URL: SALES})
    deps = make_deps(fetcher=fetcher)
    obs, _ = evidence(deps, [SALES])  # the earlier Run's Observation
    original = PassageRef(observation_id=obs.id, index=0)
    v = check(deps, background(original))
    assert v.verdict == "verified" and v.check == "historical_web_refetch"
    assert fetcher.fetched == [URL]  # the original Source, re-fetched
    assert v.original == original  # the original citation is kept
    assert v.matched is not None and v.matched.observation_id != obs.id  # a new Observation
    assert deps.repos.observations.get("alice", v.matched.observation_id).run_id == "run_new"
    assert deps.repos.passages.list("alice", obs.id)[0].text == SALES  # original untouched


def test_a_sampled_historical_pdf_claim_is_checked_against_the_original_stored_chunks(make_deps):
    fetcher = Fetcher({})
    deps = make_deps(fetcher=fetcher)
    obs, _ = evidence(deps, [SALES, "An unrelated chunk."], kind="pdf")
    v = check(deps, background(PassageRef(observation_id=obs.id, index=0)))
    assert v.verdict == "verified" and v.check == "historical_document_passages"
    assert fetcher.fetched == [] and v.matched.observation_id == obs.id


def test_an_inherited_refetch_failure_is_unchecked(make_deps):
    deps = make_deps(fetcher=Fetcher({URL: FetchError}))
    obs, _ = evidence(deps, [SALES])
    result = fact_check(deps, "alice", "run_new", historical_draft(), EvidenceBundle(sections=[]),
                        background(PassageRef(observation_id=obs.id, index=0)))
    (v,) = result.verdicts
    assert v.verdict == "unchecked" and v.check == "historical_web_refetch"
    assert result.summary["pass_rate"] is None  # left out of the pass rate
    assert not result.draft.sections[0].claims[0].text.endswith("[UNVERIFIED]")


def test_an_inherited_claim_the_page_no_longer_supports_is_unverified(make_deps):
    deps = make_deps(fetcher=Fetcher({URL: "The company opened plants in Hungary and Brazil."}))
    obs, _ = evidence(deps, [SALES])
    result = fact_check(deps, "alice", "run_new", historical_draft(), EvidenceBundle(sections=[]),
                        background(PassageRef(observation_id=obs.id, index=0)))
    (v,) = result.verdicts
    assert v.verdict == "unverified" and v.check == "historical_web_refetch"
    assert result.draft.sections[0].claims[0].text.endswith("[UNVERIFIED]")


def test_the_f2_numeric_gate_still_applies_to_inherited_checks(make_deps):
    # Similar enough to pass 0.85, but with a figure the page does not state; the inline report
    # date itself is never counted as a figure of the Claim.
    claim = ("BYD sold 4.27 million vehicles in 2025, up from 3.50 million in 2024 "
             "(reported 2026-09-01).")
    deps = make_deps(fetcher=Fetcher({URL: SALES}))
    obs, _ = evidence(deps, [SALES])
    v = check(deps, background(PassageRef(observation_id=obs.id, index=0)), claim)
    assert v.verdict == "unverified" and "does not state ['3.50']" in v.reason


def test_a_historical_claim_naming_no_historical_item_is_never_checked_against_a_guess(make_deps):
    deps = make_deps(fetcher=Fetcher({URL: SALES}))
    _, bundle = evidence(deps, [SALES])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([SALES], historical={0}), bundle).verdicts
    assert v.verdict == "unchecked" and v.check == "not_checkable"
    assert deps.fetcher.fetched == []
