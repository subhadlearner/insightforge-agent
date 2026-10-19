"""The T18 live-validation acceptance rules (tests/t18_acceptance.py), offline, on synthetic
records: each rule must reject what the review found the first harness let through."""

import hashlib
import json
import sqlite3
import subprocess
import sys
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

def body(statements, claimed=True, claim_texts=None):
    """A stored Report body: one Evidence item per statement, each cited by a Claim whose text
    is the statement (or claim_texts[n])."""
    items = [{"id": f"ev_{n}", "statement": s} for n, s in enumerate(statements)]
    texts = claim_texts or [i["statement"] for i in items]
    return {"evidence": {"sections": [{"items": items}]},
            "draft": {"gaps": ["Sub-task s2 (R&D): this research did not establish it."],
                      "sections": [{"claims": [
                          {"evidence_id": i["id"], "historical": False, "text": t}
                          for i, t in zip(items, texts)] if claimed else []}]}}


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



# ---- R3-03: obvious misstatements never count as covered ------------------------------------

def replaced(old, new):
    return [new if s == old else s for s in NORTHWIND]


CARDIO = "Cardiology devices contributed 341 million dollars of revenue in fiscal 2025."
ORTHO = "Orthopaedic implants contributed 187 million dollars of revenue in fiscal 2025."


def test_swapped_segment_figures_are_mismatches_not_coverage():
    rest = [s for s in NORTHWIND if s not in (CARDIO, ORTHO)]
    swapped = rest + ["Cardiology devices contributed 187 million dollars of revenue in fiscal 2025.",
                      "Orthopaedic implants contributed 341 million dollars of revenue in fiscal 2025."]
    r = document_coverage(body(swapped))
    assert r["status"] == "failed"
    assert (r["facts"]["cardiology segment 341 million"],
            r["facts"]["orthopaedic segment 187 million"]) == ("mismatch", "mismatch")
    one_sentence = rest + ["In fiscal 2025 cardiology contributed 187 million dollars of revenue "
                           "and orthopaedic implants 341 million dollars."]
    r = document_coverage(body(one_sentence))
    assert r["facts"]["cardiology segment 341 million"] == "mismatch"
    assert r["facts"]["orthopaedic segment 187 million"] == "mismatch"


def test_a_segment_figure_stated_as_total_revenue_is_a_mismatch():
    r = document_coverage(body(NORTHWIND + [
        "Cardiology devices revenue reached 612 million dollars in fiscal 2025."]))
    assert r["facts"]["revenue 612 million"] == "mismatch" and r["status"] == "failed"


def test_a_negated_approval_is_a_mismatch():
    r = document_coverage(body(replaced(
        "Northwind expects regulatory approval for NW-200 in the first half of 2026.",
        "Northwind does not expect regulatory approval for NW-200 in the first half of 2026.")))
    assert r["facts"]["NW-200 approval expected H1 2026"] == "mismatch" and r["status"] == "failed"


def test_another_period_is_a_mismatch_and_a_missing_period_is_only_a_candidate():
    wrong = document_coverage(body(replaced(CARDIO, CARDIO.replace("2025", "2024"))))
    assert wrong["facts"]["cardiology segment 341 million"] == "mismatch"
    assert wrong["status"] == "failed"
    unstated = document_coverage(body(replaced(CARDIO, CARDIO.replace(" in fiscal 2025", ""))))
    assert unstated["facts"]["cardiology segment 341 million"] == "candidate"
    assert unstated["status"] == "inconclusive"  # never an automatic pass


def test_a_wrong_claim_fails_even_beside_a_correct_one():
    r = document_coverage(body(NORTHWIND + [CARDIO.replace("2025", "2024")]))
    assert r["facts"]["cardiology segment 341 million"] == "mismatch"


def test_the_claim_itself_must_state_the_fact_not_only_cite_evidence_that_does():
    texts = ["Northwind is a medical device company." if s == CARDIO else s for s in NORTHWIND]
    r = document_coverage(body(NORTHWIND, claim_texts=texts))
    assert r["facts"]["cardiology segment 341 million"] == "candidate"
    assert r["status"] == "inconclusive"


def test_regulatory_claims_must_state_the_fact_and_not_negate_it():
    statements = ["Nvidia recorded $17.1 billion in annual sales to customers in China.",
                  "Nvidia took a $4.5 billion H20 inventory charge in Q1 FY2026."]
    unclaimed = regulatory_coverage(body(statements, claim_texts=[
        "Export rules for chips changed in 2025.", statements[1]]), [])
    assert unclaimed["objectives"]["Nvidia China revenue"] == "retained, not claimed"
    assert unclaimed["status"] == "failed"
    negated = regulatory_coverage(body(statements, claim_texts=[
        "Nvidia did not record $17.1 billion in sales to customers in China.", statements[1]]),
        [])
    assert negated["objectives"]["Nvidia China revenue"] != "covered"
    assert negated["status"] != "pass"


# ---- R3-02: the CLI fails closed --------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    done = subprocess.run([sys.executable, "-m", "tests.t18_acceptance", *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = None
    return done.returncode, out


@pytest.mark.parametrize("results, code, status", [
    ([judged(f"s{i}") for i in range(5)], 0, "pass"),
    ([judged(f"s{i}") for i in range(4)] + [judged("s4", "error", "string_too_long")], 1,
     "failed"),
    ([judged(f"s{i}") for i in range(4)], 2, "inconclusive"),
], ids=["pass", "failed", "inconclusive"])
def test_the_b3_command_exits_nonzero_unless_it_passes(tmp_path, results, code, status):
    f = tmp_path / "judge-report.json"
    f.write_text(json.dumps({"results": results}), encoding="utf-8")
    before = f.read_bytes()
    rc, out = cli("b3", f)
    assert (rc, out["status"]) == (code, status) and f.read_bytes() == before


def test_unreadable_input_or_usage_is_exit_3(tmp_path):
    assert cli("b3", tmp_path / "missing.json")[0] == 3
    assert cli("nonsense", tmp_path)[0] == 3


REG = ["Nvidia recorded $17.1 billion in annual sales to customers in China.",
       "Nvidia took a $4.5 billion H20 inventory charge in Q1 FY2026."]


def evaluation(tmp_path, samples):
    """A minimal finished evaluation directory: eval-report.json and anthropic/eval.db."""
    (tmp_path / "anthropic").mkdir()
    con = sqlite3.connect(tmp_path / "anthropic" / "eval.db")
    con.executescript("CREATE TABLE reports (owner_id, id, run_id, body);"
                      "CREATE TABLE run_events (run_id, type, payload);"
                      "CREATE TABLE observations (id, run_id);"
                      "CREATE TABLE passages (observation_id, idx, text);")
    entries = []
    for n, (sid, statements) in enumerate(samples):
        con.execute("INSERT INTO reports VALUES (?, ?, ?, ?)",
                    (f"eval-{sid}", f"rep_{n}", f"run_{n}", json.dumps(body(statements))))
        entries.append({"sample_id": sid, "status": "complete", "owner_id": f"eval-{sid}",
                        "report_id": f"rep_{n}", "run_id": f"run_{n}"})
    con.commit()
    con.close()
    (tmp_path / "eval-report.json").write_text(json.dumps(
        {"providers": [{"provider": "anthropic", "samples": entries}]}), encoding="utf-8")
    return tmp_path


def digest(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def test_stage_c_passes_with_exit_0_only_when_both_briefs_are_covered(tmp_path):
    d = evaluation(tmp_path, [("document-annual-report", NORTHWIND),
                              ("regulatory-impact-semiconductors", REG)])
    before = digest(d)
    rc, out = cli("stage-c", d)
    assert (rc, out["status"], out["reasons"]) == (0, "pass", [])
    assert digest(d) == before  # read-only


@pytest.mark.parametrize("samples, reason", [
    ([("regulatory-impact-semiconductors", REG)], "document-annual-report: missing"),
    ([("document-annual-report", NORTHWIND), ("document-annual-report", NORTHWIND),
      ("regulatory-impact-semiconductors", REG)], "document-annual-report: 2 results"),
], ids=["missing brief", "duplicate brief"])
def test_stage_c_requires_each_target_brief_exactly_once(tmp_path, samples, reason):
    rc, out = cli("stage-c", evaluation(tmp_path, samples))
    assert (rc, out["status"]) == (1, "failed") and reason in out["reasons"]


def test_stage_c_exit_code_reports_a_failed_brief_with_its_reasons(tmp_path):
    d = evaluation(tmp_path, [("document-annual-report", [s for s in NORTHWIND if "71" not in s]),
                              ("regulatory-impact-semiconductors", REG)])
    rc, out = cli("stage-c", d)
    assert (rc, out["status"]) == (1, "failed")
    assert "document-annual-report: R&D spending 71 million: missing" in out["reasons"]



def test_figures_inside_decimals_and_unrelated_sentences_are_not_about_the_fact():
    """Found on the T18 baseline: "6" matched inside "6.34%" and "$6.4 billion", and a sentence
    with a figure but none of the fact's terms was judged against it."""
    from tests.t18_acceptance import NORTHWIND_FACTS, judge_fact

    programmes = NORTHWIND_FACTS["6 programmes in clinical trials"]
    for unrelated in [
        "The global medical devices market is expanding at a CAGR of 6.34% between 2026 and 2035.",
        "Stryker Corporation's Q4 2024 reported net sales were $6.4 billion.",
        "The global medical devices market was valued at $739.6 billion in 2023."]:
        assert judge_fact(programmes, unrelated) == "absent", unrelated
    assert judge_fact(NORTHWIND_FACTS["R&D spending 71 million"],
                      "Revenue was 171 million dollars in fiscal 2025.") == "absent"
    assert judge_fact(programmes, "The pipeline holds 6 programmes in clinical trials at the end "
                                  "of fiscal 2025.") == "match"
