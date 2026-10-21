"""The T18 live-validation acceptance rules (tests/t18_acceptance.py), offline, on synthetic
records: each rule must reject what the review found the first harness let through."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from insightforge_agent.domain.provenance import tokens
from tests.t18_acceptance import (
    b1_breakdown,
    b1_decision,
    b1_failures,
    b1_outcome_kind,
    b2_cap_decision,
    b2_classify,
    b2_failures,
    b3_outcome,
    NorthwindDocument,
    document_coverage,
    regulatory_coverage,
)
from tests.paths import REPO_ROOT

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



# ---- B-1 diagnostics (T18 B-1 run 2, N28): how each result was reached, never gating ----------

JUDGED = "not supported: no candidate span was judged SUPPORTED ({'INSUFFICIENT_EVIDENCE': 2})"
MALFORMED = "not supported: the entailment check failed (BadModelReply)"


@pytest.mark.parametrize(("outcome", "kind"), [
    ("SUPPORTED", "supported"),
    (JUDGED, "judged"),
    ("blocked before entailment: the matched Passage does not state ['2022']", "screened"),
    ("no candidate", "screened"),
    (MALFORMED, "unusable"),
    ("not supported: the entailment reply judged none of the candidate spans", "unusable"),
    ("not supported: the entailment request (3100 tokens) exceeds the per-call cap of 3000",
     "not_sent"),
    ("something new", "unusable"),  # never counted as a valid semantic rejection
])
def test_each_b1_outcome_is_classified_by_how_it_was_reached(outcome, kind):
    assert b1_outcome_kind(outcome) == kind


def test_a_malformed_negative_passes_the_old_rule_but_is_reported_apart():
    """Runs 1 and 2 were decided by `b1_failures`, unchanged: N28 is not SUPPORTED, so it
    passed that rule. The breakdown shows it was not a semantic rejection."""
    record = b1_record(negative=lambda n: MALFORMED if n["id"] == "N28" else
                       "blocked before entailment: x" if n["id"] == "N31" else JUDGED)
    assert b1_failures(record, CASES) == []
    assert b1_breakdown(record)["negatives"] == {"judged": 38, "screened": 1, "unusable": 1}


# ---- B-1 decision from run 3 (GPT-6 decision on 773cb65): 3.3 adjudicated, unusable replies
# ---- are INCONCLUSIVE --------------------------------------------------------------------------

ADJUDICATED = json.loads((Path(__file__).resolve().parent / "fixtures" / "t18" /
                          "stage_b1_verified_adjudication.json").read_text(encoding="utf-8"))
BYD_33 = "run_06105d324454 3.3"
INSUFFICIENT = ("not supported: no candidate span was judged SUPPORTED "
                "({'INSUFFICIENT_EVIDENCE': 1})")


def b1_run3(negative=lambda n: "blocked before entailment: x" if n["id"] == "N31" else JUDGED,
            positive=lambda ref: "SUPPORTED", byd=INSUFFICIENT, other=lambda ref: "SUPPORTED"):
    record = b1_record(positive=positive, negative=negative)
    record["verified"] = [{"ref": BYD_33, "outcome": byd}] + [
        {"ref": f"v{i}", "outcome": other(f"v{i}")} for i in range(26)]
    return record


def test_the_adjudication_names_only_3_3_and_leaves_the_case_file_unchanged():
    assert {ref: a["expected"] for ref, a in ADJUDICATED["verified"].items()} == {
        BYD_33: "INSUFFICIENT_EVIDENCE"}
    assert "Source title" in ADJUDICATED["verified"][BYD_33][
        "why_the_saved_verified_is_not_authoritative"]
    raw = (Path(__file__).resolve().parent / "fixtures" / "t18" / "stage_b1_cases.json")
    assert hashlib.sha256(raw.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == (
        "704bbe19215dafb90f7e452d109b8c066cae03b94612175fc72455d8ad217b72")  # as runs 1-2 used


def test_b1_passes_only_with_26_supported_and_3_3_validly_judged_insufficient():
    assert b1_decision(b1_run3(), CASES, ADJUDICATED) == {
        "status": "PASS", "failures": [], "inconclusive": []}


@pytest.mark.parametrize("byd", [
    "SUPPORTED",
    "not supported: no candidate span was judged SUPPORTED ({'CONTRADICTED': 1})",
    "not supported: no candidate span was judged SUPPORTED "
    "({'CONTRADICTED': 1, 'INSUFFICIENT_EVIDENCE': 1})",
    "no candidate",  # fail-closed by the gates, but not a judgment
])
def test_3_3_fails_on_anything_but_a_valid_insufficient_judgment(byd):
    decision = b1_decision(b1_run3(byd=byd), CASES, ADJUDICATED)
    assert decision["status"] == "FAIL" and BYD_33 in decision["failures"][0]


def test_3_3_with_an_unusable_reply_is_inconclusive_and_its_absence_fails():
    assert b1_decision(b1_run3(byd=MALFORMED), CASES, ADJUDICATED)["status"] == "INCONCLUSIVE"
    record = b1_run3()
    record["verified"] = record["verified"][1:] + [{"ref": "v26", "outcome": "SUPPORTED"}]
    decision = b1_decision(record, CASES, ADJUDICATED)
    assert decision["status"] == "FAIL"
    assert decision["failures"] == [f"adjudicated T18-verified Claim {BYD_33}: no result recorded"]


def test_an_unusable_reply_never_satisfies_a_clean_calibration():
    """N28's shape: a malformed reply to a negative is no semantic rejection. Run 3 cannot pass
    on it; it is INCONCLUSIVE, and a definite miss elsewhere still makes it FAIL."""
    n28 = b1_run3(negative=lambda n: MALFORMED if n["id"] == "N28" else
                  "blocked before entailment: x" if n["id"] == "N31" else JUDGED)
    decision = b1_decision(n28, CASES, ADJUDICATED)
    assert decision["status"] == "INCONCLUSIVE" and decision["failures"] == []
    assert decision["inconclusive"] == [
        f"negative N28 (unsupported detail, run_f388cc91cf2a 2.1): no valid judgment ({MALFORMED})"]
    unsent = "not supported: the entailment request (3100 tokens) exceeds the per-call cap of 3000"
    assert b1_decision(b1_run3(positive=lambda r: unsent if r == "run_0b45a66fd66d 2.6" else
                               "SUPPORTED"), CASES, ADJUDICATED)["status"] == "INCONCLUSIVE"
    worse = b1_run3(negative=lambda n: MALFORMED if n["id"] == "N28" else
                    "SUPPORTED" if n["id"] == "N25" else JUDGED)
    assert b1_decision(worse, CASES, ADJUDICATED)["status"] == "FAIL"


def test_run_2s_shape_still_fails_under_the_new_decision():
    """Two Claims lost by valid judgments are definite failures, not inconclusive."""
    lost = {"v1", "v2"}
    decision = b1_decision(b1_run3(other=lambda ref: INSUFFICIENT if ref in lost else
                                   "SUPPORTED"), CASES, ADJUDICATED)
    assert decision["status"] == "FAIL" and len(decision["failures"]) == 2


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

# T18's five stored Reports, written out here independently of the rule's own table.
T18_REPORTS = [("competitive-landscape-cloud", "rep_3489459c430cef66"),
               ("document-annual-report", "rep_741a37d0ab3d8397"),
               ("market-sizing-solar", "rep_95e98f158ef7e398"),
               ("memory-prior-report", "rep_6e48a16b54b2cc51"),
               ("regulatory-impact-semiconductors", "rep_e0d086224779e53f")]
SCORES = {"coverage": 3, "organisation": 4, "synthesis": 3, "calibration": 2}


def judged(n, status="judged", reason="", scores=SCORES, sample=None, report=None):
    sid, rid = T18_REPORTS[n]
    return {"provider": "anthropic", "sample_id": sample or sid, "report_id": report or rid,
            "synthesis": {"status": status, "reason": reason,
                          "scores": scores if status == "judged" else None}}


ALL_FIVE = [judged(n) for n in range(5)]


def test_b3_passes_only_when_all_five_reports_have_valid_scores():
    assert b3_outcome({"results": ALL_FIVE}) == ("pass", [])
    status, detail = b3_outcome({"results": ALL_FIVE[:4] + [
        judged(4, "error", "invalid judge reply: string_too_long")]})
    assert status == "failed" and "string_too_long" in detail[0]  # a typed error is not a pass
    assert b3_outcome({"results": ALL_FIVE[:4]})[0] == "inconclusive"


# ---- R4-01: one complete, valid judgment for each expected Report ---------------------------

@pytest.mark.parametrize("results", [
    ALL_FIVE[:4] + [judged(3)],  # a duplicate standing in for a missing Report
    ALL_FIVE + [judged(0)],  # all five, one judged twice
    ALL_FIVE[:4] + [judged(4, sample="market-sizing-wind")],  # an unexpected sample
    ALL_FIVE[:4] + [judged(4, report="rep_000000000000")],  # another Report of the sample
], ids=["duplicate for missing", "duplicate extra", "unexpected sample", "other report"])
def test_b3_duplicates_and_unexpected_identities_fail(results):
    assert b3_outcome({"results": results})[0] == "failed"


@pytest.mark.parametrize("scores", [
    {"coverage": 3, "organisation": 4, "synthesis": 3},  # a criterion missing
    {**SCORES, "accuracy": 3},  # an extra criterion
    {**SCORES, "calibration": 0}, {**SCORES, "calibration": 6},  # out of range
    {**SCORES, "calibration": 3.5}, {**SCORES, "calibration": "3"},  # not whole numbers
    {**SCORES, "calibration": True}, {**SCORES, "calibration": None},
    {}, None,
], ids=["missing", "extra", "zero", "six", "fraction", "string", "bool", "null", "empty", "none"])
def test_b3_malformed_scores_fail(scores):
    status, detail = b3_outcome({"results": ALL_FIVE[:4] + [judged(4, scores=scores)]})
    assert status == "failed" and "regulatory-impact-semiconductors" in detail[0]


def test_b3_a_failure_outranks_a_missing_report_and_missing_alone_is_inconclusive():
    assert b3_outcome({"results": ALL_FIVE[:3] + [judged(3, scores={})]})[0] == "failed"
    status, detail = b3_outcome({"results": ALL_FIVE[1:]})
    assert (status, detail) == ("inconclusive", ["anthropic/competitive-landscape-cloud: no result"])


# ---- Stage C -----------------------------------------------------------------------------------

def body(statements, claimed=True, claim_texts=None, entity="Northwind Health", unverified=(),
         marked=True):
    """A stored Report body: one Evidence item per statement (about `entity`), each cited by a
    Claim whose text is the statement (or claim_texts[n]). `draft` is the pre-check draft;
    `verified.draft` the published Report, where each Claim at a position in `unverified` has an
    unverified verdict and, when `marked`, the [UNVERIFIED] mark. No other Claim is sampled."""
    items = [{"id": f"ev_{n}", "statement": s, "entity": entity} for n, s in enumerate(statements)]
    texts = claim_texts or [i["statement"] for i in items]
    claims = [{"evidence_id": i["id"], "historical": False, "text": t}
              for i, t in zip(items, texts)] if claimed else []
    published = [{**c, "text": c["text"] + " [UNVERIFIED]"} if n in unverified and marked else c
                 for n, c in enumerate(claims)]
    gaps = ["Sub-task s2 (R&D): this research did not establish it."]
    return {"evidence": {"sections": [{"items": items}]},
            "draft": {"gaps": gaps, "sections": [{"claims": claims}]},
            "verified": {"draft": {"gaps": gaps, "sections": [{"claims": published}]},
                         "verdicts": [{"section": 0, "claim": n, "verdict": "unverified"}
                                      for n in unverified]}}


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

ROOT = REPO_ROOT


def cli(*args):
    done = subprocess.run([sys.executable, "-m", "tests.t18_acceptance", *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = None
    return done.returncode, out


@pytest.mark.parametrize("results, code, status", [
    (ALL_FIVE, 0, "pass"),
    (ALL_FIVE[:4] + [judged(4, "error", "string_too_long")], 1, "failed"),
    (ALL_FIVE[:4] + [judged(3)], 1, "failed"),
    (ALL_FIVE[:4], 2, "inconclusive"),
], ids=["pass", "failed", "duplicate", "inconclusive"])
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
                      "CREATE TABLE sources (id, kind, title);"
                      "CREATE TABLE observations (id, run_id, source_id);"
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


# ---- R4-02: the company and the measured period ---------------------------------------------

REVENUE = "Northwind Health revenue reached 612 million dollars in fiscal 2025."
GROWTH = "Revenue grew 14 percent compared with fiscal 2024."


@pytest.mark.parametrize("claim", [
    "Medtronic revenue reached 612 million dollars in fiscal 2025.",
    "Revenue at Medtronic reached 612 million dollars in fiscal 2025.",
], ids=["sentence-initial", "mid-sentence"])
def test_a_northwind_figure_claimed_for_another_company_is_a_mismatch(claim):
    r = document_coverage(body(NORTHWIND, claim_texts=[
        claim if s == REVENUE else s for s in NORTHWIND]))
    assert r["facts"]["revenue 612 million"] == "mismatch" and r["status"] == "failed"
    beside = document_coverage(body(NORTHWIND + [REVENUE], claim_texts=NORTHWIND + [claim]))
    assert beside["facts"]["revenue 612 million"] == "mismatch"  # even beside the right Claim


def test_a_claim_citing_evidence_about_another_company_is_a_mismatch():
    northwind = body(NORTHWIND)
    other = {"id": "ev_other", "entity": "Medtronic",
             "statement": "Medtronic revenue reached 612 million dollars in fiscal 2025."}
    northwind["evidence"]["sections"][0]["items"].append(other)
    northwind["verified"]["draft"]["sections"][0]["claims"].append(
        {"evidence_id": "ev_other", "historical": False,
         "text": "Revenue reached 612 million dollars in fiscal 2025."})
    assert document_coverage(northwind)["facts"]["revenue 612 million"] == "mismatch"


def test_implicit_attribution_comes_only_from_the_cited_evidence():
    # "Revenue grew 14 percent ..." names no company: its Northwind Evidence attributes it.
    assert document_coverage(body(NORTHWIND))["facts"]["growth 14 percent"] == "covered"
    unattributed = document_coverage(body(NORTHWIND, entity=""))
    assert unattributed["facts"]["growth 14 percent"] == "candidate"
    assert unattributed["facts"]["revenue 612 million"] == "covered"  # names Northwind itself
    assert unattributed["status"] == "inconclusive"


@pytest.mark.parametrize("claim, verdict", [
    ("Revenue grew 14 percent in fiscal 2024.", "mismatch"),  # the comparison year as the period
    ("Revenue grew 14 percent in fiscal 2025 compared with fiscal 2023.", "mismatch"),
    ("Revenue grew 14 percent compared with fiscal 2023.", "mismatch"),  # another base
    ("Revenue grew 14 percent.", "candidate"),  # no period stated
    (GROWTH, "covered"),  # the base fixes the period: the year after it
    ("Revenue grew 14 percent in fiscal 2025 compared with fiscal 2024.", "covered"),
    ("Northwind Health revenue rose 14 percent over fiscal 2024.", "covered"),
])
def test_growth_separates_the_measured_period_from_the_comparison_base(claim, verdict):
    texts = [claim if s == GROWTH else s for s in NORTHWIND]
    assert document_coverage(body(NORTHWIND, claim_texts=texts))["facts"][
        "growth 14 percent"] == verdict


# ---- R4-03: the published Report, and the Fact-Checker's verdicts ---------------------------

@pytest.mark.parametrize("marked", [True, False], ids=["marked", "verdict only"])
def test_a_fact_claimed_only_by_an_unverified_claim_is_not_a_pass(marked):
    r = document_coverage(body(NORTHWIND, unverified=(0,), marked=marked))
    assert r["facts"]["revenue 612 million"] == "unverified claim"
    assert r["status"] == "inconclusive"


def test_the_published_report_is_read_not_the_pre_check_draft():
    b = body(NORTHWIND)
    published = b["verified"]["draft"]["sections"][0]
    published["claims"] = [c for c in published["claims"] if "612" not in c["text"]]
    r = document_coverage(b)  # the pre-check draft still states it
    assert r["facts"]["revenue 612 million"] == "retained, not claimed" and r["status"] == "failed"


def test_unsampled_claims_count_as_published_and_no_verdict_is_invented():
    b = body(NORTHWIND)
    assert b["verified"]["verdicts"] == []
    assert document_coverage(b)["status"] == "pass"


def test_an_unverified_misstatement_still_fails():
    texts = [CARDIO.replace("2025", "2024") if s == CARDIO else s for s in NORTHWIND]
    r = document_coverage(body(NORTHWIND, claim_texts=texts,
                               unverified=(NORTHWIND.index(CARDIO),)))
    assert r["facts"]["cardiology segment 341 million"] == "mismatch"


def test_historical_claims_never_cover_a_current_fact():
    b = body(NORTHWIND)
    for c in b["verified"]["draft"]["sections"][0]["claims"]:
        if "612" in c["text"]:
            c["historical"] = True
    assert document_coverage(b)["facts"]["revenue 612 million"] == "retained, not claimed"


def test_regulatory_coverage_needs_a_claim_that_was_not_marked_unverified():
    r = regulatory_coverage(body(REG, unverified=(0,)), [])
    assert r["objectives"] == {"Nvidia China revenue": "unverified claim",
                               "H20 charge or lost sales": "covered"}
    assert r["status"] == "inconclusive"


def test_stage_c_command_exits_2_when_a_fact_rests_on_an_unverified_claim(tmp_path):
    d = evaluation(tmp_path, [("document-annual-report", NORTHWIND),
                              ("regulatory-impact-semiconductors", REG)])
    con = sqlite3.connect(d / "anthropic" / "eval.db")
    con.execute("UPDATE reports SET body=? WHERE id='rep_0'",
                (json.dumps(body(NORTHWIND, unverified=(0,))),))
    con.commit()
    con.close()
    rc, out = cli("stage-c", d)
    assert (rc, out["status"]) == (2, "inconclusive")
    assert "document-annual-report: revenue 612 million: unverified claim" in out["reasons"]


# ---- Approval expected, not approval granted ------------------------------------------------

APPROVAL = "Northwind expects regulatory approval for NW-200 in the first half of 2026."


@pytest.mark.parametrize("claim, verdict", [
    (APPROVAL, "covered"),
    ("NW-200 is on track for regulatory approval in H1 2026.", "covered"),
    ("Northwind anticipates that NW-200 will receive approval in the first half of 2026.",
     "covered"),
    ("Northwind expects NW-200 approval to be granted in the first half of 2026.", "covered"),
    # Approval already granted is not the forecast the document states.
    ("NW-200 received regulatory approval in the first half of 2026.", "mismatch"),
    ("NW-200 was approved by regulators in the first half of 2026.", "mismatch"),
    ("NW-200 was granted regulatory approval in H1 2026.", "mismatch"),
    ("Northwind has obtained approval for NW-200 in the first half of 2026.", "mismatch"),
    # Neither, or both: the rules cannot settle it.
    ("NW-200 regulatory approval in the first half of 2026.", "candidate"),
    ("NW-200 received approval in the first half of 2026, earlier than expected.", "candidate"),
])
def test_an_approval_already_granted_is_not_the_expected_approval(claim, verdict):
    texts = [claim if s == APPROVAL else s for s in NORTHWIND]
    assert document_coverage(body(NORTHWIND, claim_texts=texts))["facts"][
        "NW-200 approval expected H1 2026"] == verdict


# ---- Stage C rerun: attribution through the uploaded report, and contributing segments ------
# The live Run's Evidence named segments and products as its entity ("Cardiology devices",
# "NW-200"), which the rules read as another company (run_61c2b5c7ec63, rep_5cb300569eb6f65b).

DOC_ENTITIES = ["Northwind Health", "Northwind Health", "Cardiology devices", "Orthopaedic implants",
                "Diagnostics", "Northwind Health", "Northwind Health", "NW-200", "NW-200",
                "Orthopaedic implant titanium supply", "Average selling prices", "NW-200",
                "Northwind Health"]
NORTHWIND_PDF = NorthwindDocument({("obs_doc", n): s for n, s in enumerate(NORTHWIND)},
                                  frozenset(w for s in NORTHWIND for w in tokens(s) if w.islower())
                                  | {"delays", "pricing"})  # as the Run's web pages write them


def from_document(b, entities=DOC_ENTITIES, observation="obs_doc"):
    """The body with each Evidence item's entity as the live Run set it, each read from its own
    Passage of `observation` (the report states each fact in its own Passage)."""
    for n, (item, entity) in enumerate(zip(b["evidence"]["sections"][0]["items"], entities)):
        index = NORTHWIND.index(item["statement"]) if item["statement"] in NORTHWIND else n
        item.update(entity=entity, primary={"observation_id": observation, "index": index})
    return b


def test_segment_and_product_entities_read_from_the_northwind_report_are_northwind_facts():
    r = document_coverage(from_document(body(NORTHWIND)), NORTHWIND_PDF)
    assert r["status"] == "pass", r["facts"]
    # Without that provenance the same entities are still read as another company.
    assert document_coverage(from_document(body(NORTHWIND)))["facts"][
        "cardiology segment 341 million"] == "mismatch"
    elsewhere = document_coverage(from_document(body(NORTHWIND), observation="obs_web"),
                                  NORTHWIND_PDF)
    assert elsewhere["facts"]["cardiology segment 341 million"] == "mismatch"


def test_a_sentence_initial_word_the_rules_cannot_place_is_only_a_candidate():
    # The live Run's delay risk: "Delays" opens the sentence and no Passage writes it in lower
    # case, so it may be a name. It is left for manual adjudication, never passed.
    document = NorthwindDocument(NORTHWIND_PDF.passages, NORTHWIND_PDF.common - {"delays"})
    r = document_coverage(from_document(body(NORTHWIND)), document)
    assert r["facts"]["NW-200 delay risk"] == "candidate" and r["status"] == "inconclusive"


def with_competitor(passage, statement, entity):
    """The Northwind body without its revenue item, plus one Evidence item read from a Passage
    of the Northwind report itself that discusses a competitor, cited by the only revenue Claim,
    which names no company. "covered" would mean the competitor's figure was credited."""
    b = from_document(body([s for s in NORTHWIND if s != REVENUE]),
                      [e for s, e in zip(NORTHWIND, DOC_ENTITIES) if s != REVENUE])
    b["evidence"]["sections"][0]["items"].append({
        "id": "ev_other", "entity": entity, "statement": statement,
        "primary": {"observation_id": "obs_doc", "index": len(NORTHWIND)}})
    b["verified"]["draft"]["sections"][0]["claims"].append(
        {"evidence_id": "ev_other", "historical": False,
         "text": "Revenue reached 612 million dollars in fiscal 2025."})
    document = NorthwindDocument({**NORTHWIND_PDF.passages, ("obs_doc", len(NORTHWIND)): passage},
                                 NORTHWIND_PDF.common)
    return document_coverage(b, document)["facts"]["revenue 612 million"]


COMPETITOR = "Its competitor Medtronic reported that revenue reached 612 million dollars in fiscal 2025."
SAME = "Revenue reached 612 million dollars in fiscal 2025."


@pytest.mark.parametrize("passage, statement, entity, verdict", [
    # The report discusses the competitor and the item says so: a mismatch.
    ("Medtronic revenue reached 612 million dollars in fiscal 2025.",
     "Medtronic revenue reached 612 million dollars in fiscal 2025.", "Medtronic", "mismatch"),
    ("Revenue at Medtronic reached 612 million dollars in fiscal 2025.",
     "Revenue at Medtronic reached 612 million dollars in fiscal 2025.", "", "mismatch"),
    # The item hides the competitor, but the Passage it cites names it: never credited.
    (COMPETITOR, SAME, "", "candidate"),
    (COMPETITOR, SAME, "Revenue", "candidate"),
    # Only a sentence-initial name the rules cannot place: manual adjudication, not a pass.
    ("Medtronic revenue reached 612 million dollars in fiscal 2025.",
     "Medtronic revenue reached 612 million dollars in fiscal 2025.", "", "candidate"),
], ids=["entity", "mid-sentence", "passage-only", "passage-only-generic-entity",
        "sentence-initial-only"])
def test_a_competitor_discussed_in_the_northwind_report_is_never_credited_to_northwind(
        passage, statement, entity, verdict):
    assert with_competitor(passage, statement, entity) == verdict

@pytest.mark.parametrize("claim, verdict", [
    ("Northwind Health revenue grew 14 percent in fiscal 2025 compared with fiscal 2024, led by "
     "the cardiology segment.", "covered"),  # the live Claim: cardiology drove the total
    ("Revenue grew 14 percent compared with fiscal 2024, driven by cardiology.", "covered"),
    ("Cardiology revenue grew 14 percent compared with fiscal 2024.", "mismatch"),
    ("Revenue grew 14 percent in the cardiology segment compared with fiscal 2024.", "mismatch"),
    ("Revenue grew 14 percent compared with fiscal 2024, led by the cardiology segment, which "
     "grew 20 percent.", "covered"),
])
def test_a_segment_named_only_as_what_drove_total_growth_does_not_own_it(claim, verdict):
    texts = [claim if s == GROWTH else s for s in NORTHWIND]
    assert document_coverage(body(NORTHWIND, claim_texts=texts))["facts"][
        "growth 14 percent"] == verdict


def test_stage_c_command_attributes_through_the_runs_uploaded_report(tmp_path):
    d = evaluation(tmp_path, [("document-annual-report", NORTHWIND),
                              ("regulatory-impact-semiconductors", REG)])
    con = sqlite3.connect(d / "anthropic" / "eval.db")
    con.execute("UPDATE reports SET body=? WHERE id='rep_0'",
                (json.dumps(from_document(body(NORTHWIND))),))
    con.execute("INSERT INTO sources VALUES ('src_doc', 'pdf', 'northwind-health-annual-report-2025.pdf')")
    con.execute("INSERT INTO observations VALUES ('obs_doc', 'run_0', 'src_doc')")
    con.executemany("INSERT INTO passages VALUES ('obs_doc', ?, ?)", enumerate(NORTHWIND))
    # A web page the Run also read, which writes "pricing" and "delays" in lower case.
    con.execute("INSERT INTO observations VALUES ('obs_web', 'run_0', 'src_web')")
    con.execute("INSERT INTO passages VALUES ('obs_web', 0, 'Device makers face approval "
                "delays and pricing pressure this year.')")
    con.commit()
    con.close()
    rc, out = cli("stage-c", d)
    assert (rc, out["status"]) == (0, "pass"), out


# ---- Final pre-Stage-C review: naming Northwind, or a code with digits, is not enough --------

def with_item(passage, statement, entity, claim):
    """`with_competitor`, with the Claim's own text given."""
    b = from_document(body([s for s in NORTHWIND if s != REVENUE]),
                      [e for s, e in zip(NORTHWIND, DOC_ENTITIES) if s != REVENUE])
    b["evidence"]["sections"][0]["items"].append({
        "id": "ev_x", "entity": entity, "statement": statement,
        "primary": {"observation_id": "obs_doc", "index": 99}})
    b["verified"]["draft"]["sections"][0]["claims"].append(
        {"evidence_id": "ev_x", "historical": False, "text": claim})
    document = NorthwindDocument({**NORTHWIND_PDF.passages, ("obs_doc", 99): passage},
                                 NORTHWIND_PDF.common)
    return document_coverage(b, document)["facts"]["revenue 612 million"]


UNLIKE = "Unlike Northwind, Medtronic revenue reached 612 million dollars in fiscal 2025."
CODE = "3M revenue reached 612 million dollars in fiscal 2025."


@pytest.mark.parametrize("passage, statement, entity, claim, verdict", [
    # Another company's figure in a Passage that also mentions Northwind.
    (UNLIKE, UNLIKE, "Medtronic", UNLIKE, "candidate"),
    (UNLIKE, UNLIKE, "Northwind Health", UNLIKE, "candidate"),
    (UNLIKE, UNLIKE, "", SAME, "candidate"),
    # Another company or product whose name has digits.
    (CODE, CODE, "3M", SAME, "mismatch"),
    (CODE, CODE, "", SAME, "candidate"),
    ("The rival AB-500 maker reported that revenue reached 612 million dollars in fiscal 2025.",
     "AB-500 revenue reached 612 million dollars in fiscal 2025.", "AB-500", SAME, "mismatch"),
], ids=["names-both-entity-other", "names-both-entity-northwind", "names-both-claim-silent",
        "digits-entity", "digits-sentence-initial", "digits-product-code"])
def test_naming_northwind_or_a_digit_code_never_credits_another_companys_figure(
        passage, statement, entity, claim, verdict):
    assert with_item(passage, statement, entity, claim) == verdict
