"""The live support gate's decision, offline (tests/research_integrity_gate.py). No model call."""

from tests.research_integrity_gate import Case, decide, gold_cases


def cases():
    return [Case("gold", "P", "s", "p", "SUPPORTED"),
            Case("gold", "N", "s", "p", "NOT_SUPPORTED", repeats=3),
            Case("b1_positive", "partly", "s", "p", "REPORT", meta={"adjudication": "partly"})]


def rows(p="SUPPORTED", n=("INSUFFICIENT_EVIDENCE",) * 3):
    return [{"set": "gold", "id": "P", "repeat": 0, "verdict": p}] + [
        {"set": "gold", "id": "N", "repeat": i, "verdict": v} for i, v in enumerate(n)]


def test_all_correct_is_a_pass_and_reported_cases_never_gate():
    out = decide(cases(), rows())
    assert out["status"] == "PASS" and out["failures"] == [] and len(out["reported"]) == 1


def test_one_supported_negative_in_any_repetition_fails():
    out = decide(cases(), rows(n=("INSUFFICIENT_EVIDENCE", "SUPPORTED", "CONTRADICTED")))
    assert out["status"] == "FAIL" and "1 of 3" in out["failures"][0]


def test_a_rejected_positive_fails():
    assert decide(cases(), rows(p="INSUFFICIENT_EVIDENCE"))["status"] == "FAIL"


def test_a_verification_error_or_a_missing_repetition_is_inconclusive_never_a_pass():
    assert decide(cases(), rows(p="VERIFICATION_ERROR"))["status"] == "INCONCLUSIVE"
    assert decide(cases(), rows(n=("CONTRADICTED",)))["status"] == "INCONCLUSIVE"


def test_the_gold_set_repeats_only_negatives_the_code_cannot_stop_alone():
    built = {c.id: c for c in gold_cases(repeats=3)}
    assert built["NW-P01"].repeats == 1 and built["NW-P01"].entity == "Northwind Health"
    assert built["Q-N02"].repeats == 3 and built["Q-N02"].expected == "NOT_SUPPORTED"
    assert built["NW-N07"].repeats == 3  # competitor sentence in the company's own report
    assert all(c.repeats == 1 for c in built.values() if c.expected == "SUPPORTED")
