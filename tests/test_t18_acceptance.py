"""The T18 live-validation acceptance rules (tests/t18_acceptance.py), offline, on synthetic
records: each rule must reject what the review found the first harness let through."""

import json
from pathlib import Path

import pytest

from tests.t18_acceptance import (
    b1_failures,
    b2_cap_decision,
    b2_classify,
    b2_failures,
    b3_outcome,
    document_coverage,
    regulatory_coverage,
)

CASES = json.loads((Path(__file__).resolve().parent / "fixtures" / "t18" /
                    "stage_b1_cases.json").read_text(encoding="utf-8"))


def b1_record(positive=lambda ref: "SUPPORTED", negative=lambda case: "not supported: x",
              verified=27):
    return {
        "positives": [{"ref": g["ref"], "outcome": positive(g["ref"])} for g in CASES["groups"]],
        "negatives": [{"ref": g["ref"], "id": n["id"], "outcome": negative(n)}
                      for g in CASES["groups"] for n in g["negatives"]],
        "verified": [{"ref": f"v{i}", "outcome": "SUPPORTED"} for i in range(verified)],
    }


# ---- B-1 ---------------------------------------------------------------------------------------

def test_b1_passes_only_a_complete_clean_calibration():
    assert b1_failures(b1_record(), CASES) == []


def test_b1_fails_when_a_clearly_supported_positive_is_not_supported_and_names_it():
    stryker = "run_0b45a66fd66d 2.6"
    failures = b1_failures(b1_record(
        positive=lambda ref: "not supported: no candidate span was judged SUPPORTED"
        if ref == stryker else "SUPPORTED"), CASES)
    assert len(failures) == 1
    assert stryker in failures[0] and "Stryker Corporation's full year" in failures[0]


@pytest.mark.parametrize("ref", ["run_f388cc91cf2a 1.1", "run_fc1c9560577b 3.1"],
                         ids=["Germany partly", "H20 green zone doubtful"])
def test_b1_reports_but_does_not_gate_the_two_non_clear_positives(ref):
    assert b1_failures(b1_record(positive=lambda r: "not supported" if r == ref else "SUPPORTED"),
                       CASES) == []


def test_b1_fails_on_a_supported_negative_a_lost_verified_claim_or_a_missing_case():
    assert "N38" in b1_failures(b1_record(
        negative=lambda n: "SUPPORTED" if n["id"] == "N38" else "x"), CASES)[0]
    assert b1_failures(b1_record(verified=26), CASES) == [
        "26 of the 27 T18-verified Claims recorded"]
    incomplete = b1_record()
    incomplete["negatives"] = incomplete["negatives"][1:]
    assert b1_failures(incomplete, CASES) == [
        "negative N01 (run_06105d324454 0.1): no result recorded"]


# ---- B-2 ---------------------------------------------------------------------------------------

OK = {"passages": 100, "items": 20}


@pytest.mark.parametrize("row, kind", [
    (OK, "ok"),
    ({"kind": "incomplete_json", "stop_reason": "max_tokens"}, "truncated"),
    ({"kind": "empty", "stop_reason": "max_tokens"}, "truncated"),  # all reasoning, cut off
    ({"kind": "incomplete_json", "stop_reason": "end_turn"}, "failure"),  # not a truncation
    ({"kind": "invalid_json", "stop_reason": "end_turn"}, "failure"),
    ({"kind": "schema", "stop_reason": "end_turn"}, "failure"),
    ({"kind": "empty", "stop_reason": "refusal"}, "failure"),
    ({"kind": "rejected", "detail": "400"}, "failure"),
])
def test_b2_counts_truncation_only_when_the_provider_stopped_at_max_tokens(row, kind):
    assert b2_classify(row) == kind


def test_b2_applies_the_syntax_rule_to_every_cap_configuration():
    record = {"None": [OK], "150": [OK, {"kind": "invalid_json", "stop_reason": "end_turn"}],
              "120": [OK]}
    (failure,) = b2_failures(record)
    assert failure.startswith("cap 150, call 2:")
    assert b2_failures({"None": [OK], "150": [OK]}) == ["cap 120: no calls recorded"]


def test_b2_cap_decision_follows_the_spec_rule():
    trunc = {"kind": "incomplete_json", "stop_reason": "max_tokens"}
    assert b2_cap_decision({"None": [OK], "150": [OK], "120": [OK]}) == "None"
    assert b2_cap_decision({"None": [OK, trunc], "150": [OK, OK], "120": [OK, OK, OK]}) == "150"
    assert b2_cap_decision({"None": [OK, trunc], "150": [trunc], "120": [trunc]}) == "undecided"


# ---- B-3 ---------------------------------------------------------------------------------------

def judged(sid, status="judged", reason=""):
    return {"sample_id": sid, "synthesis": {"status": status, "reason": reason,
                                            "scores": {"coverage": 3} if status == "judged" else None}}


def test_b3_passes_only_when_all_five_reports_have_valid_scores():
    assert b3_outcome({"results": [judged(f"s{i}") for i in range(5)]}) == ("pass", [])
    status, detail = b3_outcome({"results": [judged(f"s{i}") for i in range(4)] + [
        judged("s4", "error", "invalid judge reply: string_too_long")]})
    assert status == "failed" and "string_too_long" in detail[0]  # a typed error is not a pass
    assert b3_outcome({"results": [judged(f"s{i}") for i in range(4)]})[0] == "inconclusive"


# ---- Stage C -----------------------------------------------------------------------------------

def body(statements, claimed=True):
    items = [{"id": f"ev_{n}", "statement": s} for n, s in enumerate(statements)]
    return {"evidence": {"sections": [{"items": items}]},
            "draft": {"gaps": ["Sub-task s2 (R&D): this research did not establish it."],
                      "sections": [{"claims": [{"evidence_id": i["id"], "historical": False}
                                               for i in items] if claimed else []}]}}


NORTHWIND = ["Northwind Health revenue reached 612 million dollars in fiscal 2025.",
             "Revenue grew 14 percent compared with fiscal 2024.",
             "Cardiology devices contributed 341 million dollars of revenue in fiscal 2025.",
             "Orthopaedic implants contributed 187 million dollars of revenue in fiscal 2025.",
             "Diagnostics contributed 84 million dollars of revenue in fiscal 2025.",
             "Research and development spending was 71 million dollars in fiscal 2025.",
             "The pipeline holds 6 programmes in clinical trials at the end of fiscal 2025.",
             "The NW-200 heart valve completed its pivotal trial in September 2025.",
             "Northwind expects regulatory approval for NW-200 in the first half of 2026.",
             "A single supplier provides the titanium used in orthopaedic implants.",
             "Pricing pressure from hospital purchasing groups reduced average selling prices.",
             "Delays to the NW-200 approval would defer expected cardiology revenue.",
             "Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent."]


def test_stage_c_document_passes_only_when_every_fact_is_retained_and_claimed():
    assert document_coverage(body(NORTHWIND))["status"] == "pass"
    missing = document_coverage(body([s for s in NORTHWIND if "71 million" not in s]))
    assert missing["status"] == "failed"
    assert missing["facts"]["R&D spending 71 million"] == "missing"  # the Gap line does not count
    unclaimed = document_coverage(body(NORTHWIND, claimed=False))
    assert unclaimed["status"] == "failed"
    assert set(unclaimed["facts"].values()) == {"retained, not claimed"}


def test_stage_c_regulatory_separates_pipeline_loss_from_unfetched_sources():
    covered = body(["Nvidia recorded $17.1 billion in annual sales to customers in China.",
                    "Nvidia took a $4.5 billion H20 inventory charge in Q1 FY2026."])
    assert regulatory_coverage(covered, [])["status"] == "pass"
    sharing = body(["Nvidia and AMD agreed to share 15% of revenues from chip sales to China."])
    lost = regulatory_coverage(sharing, [
        "China represented $17 billion in NVIDIA revenue in the fiscal year.",
        "NVIDIA disclosed a $4.5 billion charge tied to H20 excess inventory."])
    assert lost == {"status": "failed", "objectives": {
        "Nvidia China revenue": "pipeline loss", "H20 charge or lost sales": "pipeline loss"}}
    unfetched = regulatory_coverage(sharing, ["Unrelated text about export rules."])
    assert unfetched["status"] == "inconclusive"
    assert set(unfetched["objectives"].values()) == {"not fetched"}
