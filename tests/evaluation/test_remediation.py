"""The section 11 rule (design.md): decided on the Anthropic run of the full library. Only a
run whose every brief was measured can be `required` or `not_required`; anything less is
`undetermined`, with the rate it did measure and whether that rate is below the threshold."""

from insightforge_agent.evaluation.judge import not_judged
from insightforge_agent.evaluation.metrics import FactCheckMetrics
from insightforge_agent.evaluation.report import LibraryInfo, SampleResult, remediation_of, totals_of

IDS = ["a", "b", "c", "d", "e"]
FULL = LibraryInfo(directory="samples/briefs", briefs_in_library=5, sample_ids=IDS,
                   fingerprint="f")


def measured(sid: str, verified: int, unverified: int, unchecked: int = 0) -> SampleResult:
    return SampleResult(sample_id=sid, industry="i", question_type="q", status="complete",
                        owner_id=f"eval-{sid}", run_id=f"run-{sid}",
                        fact_check=FactCheckMetrics(
                            claims=10, sampled=verified + unverified + unchecked,
                            verified=verified, unverified=unverified, unchecked=unchecked),
                        synthesis=not_judged("none"))


def failed(sid: str) -> SampleResult:
    return SampleResult(sample_id=sid, industry="i", question_type="q", status="failed",
                        owner_id=f"eval-{sid}", run_id=f"run-{sid}",
                        reason="every Sub-task failed", synthesis=not_judged("no Report"))


def decide(samples, library=FULL, provider="anthropic"):
    return remediation_of(provider, samples, totals_of(samples), library)


def test_full_library_one_failed_run_below_the_threshold_is_undetermined_not_required():
    samples = [measured(s, 3, 2) for s in IDS[:4]] + [failed("e")]  # 12 of 20: 60%
    r = decide(samples)
    assert r.status == "undetermined"
    assert "60.0%" in r.reason and "below 80%" in r.reason and "e" in r.reason


def test_full_library_one_failed_run_above_the_threshold_is_undetermined():
    samples = [measured(s, 5, 0) for s in IDS[:4]] + [failed("e")]
    r = decide(samples)
    assert r.status == "undetermined" and "100.0%" in r.reason and "below" not in r.reason


def test_full_library_all_measured_below_the_threshold_is_required():
    r = decide([measured(s, 3, 2) for s in IDS])
    assert r.status == "required" and "60.0%" in r.reason


def test_full_library_all_measured_at_exactly_the_threshold_is_not_required():
    r = decide([measured(s, 4, 1) for s in IDS])  # 20 of 25 = 80%
    assert r.status == "not_required" and "80.0%" in r.reason


def test_full_library_with_no_checkable_claims_is_undetermined():
    r = decide([measured(s, 0, 0, unchecked=5) for s in IDS])
    assert r.status == "undetermined" and "none" in r.reason


def test_a_partial_run_stays_undetermined_whatever_it_measured():
    partial = FULL.model_copy(update={"sample_ids": ["a"]})
    r = decide([measured("a", 3, 2)], library=partial)
    assert r.status == "undetermined" and "partial run" in r.reason and "below 80%" in r.reason


def test_other_providers_are_not_applicable():
    assert decide([measured(s, 3, 2) for s in IDS], provider="gemini").status == "not_applicable"
