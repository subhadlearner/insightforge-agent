"""Tier 2 (recorded replay): M1.3 reporting corrections. Predictions are recorded for a stated
input view, and a report accepts only the view it was asked for."""

import pytest

from insightforge_agent.benchmark import baseline as b
from insightforge_agent.benchmark.cases import InputAccess
from insightforge_agent.benchmark.cli import main as bench
from insightforge_agent.benchmark.judge import JudgeStatus, Usage, Verdict
from insightforge_agent.benchmark.recording import RecordingStore, RunIdentity
from insightforge_agent.benchmark.report import build_report, render_text

from .helpers import make_case, reviewed, write_jsonl

pytestmark = pytest.mark.replay

PAIR_INPUTS = {
    "statement_a": "BYD sold 4.27 million vehicles in 2025.",
    "statement_b": "BYD delivered 4,270,000 cars in 2025.",
    "structured_a": {"entity": "BYD", "predicate": "units_sold", "value": "4.27 million",
                     "scope": "global", "period": "2025"},
    "structured_b": {"entity": "BYD", "predicate": "units_sold", "value": "4,270,000",
                     "scope": "global", "period": "2025"},
}


def record(store, case, access, label=None, status="JUDGED", reason="because", diagnostics=None):
    view = case.prediction_view(access)
    kw = {"label": label} if status == "JUDGED" else {"reason": reason}
    store.record(
        RunIdentity(candidate="cand", candidate_version="1", model="m", run="r1"), view,
        Verdict(case_id=view.id, case_type=view.type, status=status, usage=Usage(),
                diagnostics=diagnostics or {}, **kw))


def reviewed_case(case_type, case_id, **kw):
    return make_case(case_type, case_id, **{**reviewed(), **kw})


# ---- strict input view -----------------------------------------------------------------------

@pytest.mark.parametrize("recorded, asked, stale", [
    (InputAccess.BASELINE, InputAccess.BASELINE, False),
    (InputAccess.SEMANTIC, InputAccess.SEMANTIC, False),
    (InputAccess.BASELINE, InputAccess.SEMANTIC, True),
    (InputAccess.SEMANTIC, InputAccess.BASELINE, True),
])
def test_a_report_accepts_only_the_input_view_it_was_asked_for(tmp_path, recorded, asked, stale):
    case = reviewed_case("PAIR", "p1", inputs=PAIR_INPUTS)
    store = RecordingStore(tmp_path)
    record(store, case, recorded, label="SAME_FACT")
    report = build_report([case], store, "cand", "r1", access=asked)
    g = report.overall["PAIR"]
    assert (g.stale, g.judged) == ((1, 0) if stale else (0, 1))
    assert report.access == asked.value
    assert any("stale" in n for n in report.notices) is stale


def test_non_pair_cases_are_also_bound_to_their_view(tmp_path):
    case = reviewed_case("INDEPENDENCE", "i1")
    store = RecordingStore(tmp_path)
    record(store, case, InputAccess.SEMANTIC, label="INDEPENDENT")
    assert build_report([case], store, "cand", "r1", access=InputAccess.BASELINE
                        ).overall["INDEPENDENCE"].stale == 1


# ---- false contradiction, apart from false merge and false support ---------------------------

def test_false_contradiction_is_separate_from_false_merge_for_pairs(tmp_path):
    store = RecordingStore(tmp_path)
    cases = [reviewed_case("PAIR", "n1", expected="DIFFERENT_FACT"),
             reviewed_case("PAIR", "n2", expected="DIFFERENT_FACT"),
             reviewed_case("PAIR", "c1", expected="CONTRADICTORY"),
             reviewed_case("PAIR", "p1", expected="SAME_FACT")]
    record(store, cases[0], InputAccess.SEMANTIC, label="CONTRADICTORY")   # false contradiction
    record(store, cases[1], InputAccess.SEMANTIC, label="DIFFERENT_FACT")  # correct
    record(store, cases[2], InputAccess.SEMANTIC, label="CONTRADICTORY")   # correct, not a false one
    record(store, cases[3], InputAccess.SEMANTIC, label="CONTRADICTORY")   # false contradiction
    g = build_report(cases, store, "cand", "r1").overall["PAIR"]
    fc = g.false_contradiction
    assert (fc.numerator, fc.denominator) == (2, 3)  # 3 judged cases not expected CONTRADICTORY
    assert g.false_positives == 0                    # no false merge
    assert "false contradiction" in render_text(build_report(cases, store, "cand", "r1"))


def test_false_contradiction_is_separate_from_false_support(tmp_path):
    store = RecordingStore(tmp_path)
    cases = [reviewed_case("SUPPORT", "s1", expected="INSUFFICIENT_EVIDENCE"),
             reviewed_case("SUPPORT", "s2", expected="CONTRADICTED")]
    record(store, cases[0], InputAccess.SEMANTIC, label="CONTRADICTED")
    record(store, cases[1], InputAccess.SEMANTIC, label="SUPPORTED")  # false support, not a false contradiction
    g = build_report(cases, store, "cand", "r1").overall["SUPPORT"]
    assert (g.false_contradiction.numerator, g.false_contradiction.denominator) == (1, 1)
    assert g.false_positives == 1


def test_false_contradiction_does_not_apply_to_grounding(tmp_path):
    case = reviewed_case("INDEPENDENCE", "i1")
    store = RecordingStore(tmp_path)
    record(store, case, InputAccess.SEMANTIC, label="INDEPENDENT")
    assert build_report([case], store, "cand", "r1").overall["INDEPENDENCE"].false_contradiction is None


# ---- GROUND subtypes -------------------------------------------------------------------------

def ground(case_id, check, expected="NOT_GROUNDED"):
    inputs = {"check": check, "claim": "Acme revenue grew 10%.",
              "passage_text": "Acme revenue grew 10% in 2024.",
              "passage_ref": {"observation_id": "o", "index": 0}}
    if check == "QUOTED_SPAN":
        inputs["quoted_span"] = "revenue grew 10%"
    return reviewed_case("GROUND", case_id, expected=expected, inputs=inputs)


def test_ground_subtypes_are_reported_independently(tmp_path):
    store = RecordingStore(tmp_path)
    cases = [ground("q1", "QUOTED_SPAN"), ground("q2", "QUOTED_SPAN"), ground("c1", "CLAIM_PASSAGE")]
    record(store, cases[0], InputAccess.SEMANTIC, label="NOT_GROUNDED")
    record(store, cases[1], InputAccess.SEMANTIC, label="GROUNDED")   # an ungrounded Claim passed
    record(store, cases[2], InputAccess.SEMANTIC, label="NOT_GROUNDED")
    report = build_report(cases, store, "cand", "r1")
    quoted, claim = report.by_ground_check["GROUND/QUOTED_SPAN"], report.by_ground_check["GROUND/CLAIM_PASSAGE"]
    assert (quoted.eligible, quoted.false_positives) == (2, 1)
    assert (claim.eligible, claim.false_positives) == (1, 0)
    assert any("GROUND combines two different checks" in n for n in report.notices)
    assert "GROUND by check" in render_text(report)


# ---- accounting and honesty ------------------------------------------------------------------

def test_missing_abstained_and_not_applicable_predictions_are_all_counted_with_reasons(tmp_path):
    store = RecordingStore(tmp_path)
    cases = [reviewed_case("SUPPORT", f"s{n}", expected="SUPPORTED") for n in range(5)]
    record(store, cases[0], InputAccess.SEMANTIC, label="SUPPORTED")
    record(store, cases[1], InputAccess.SEMANTIC, status="ABSTAIN", reason="unsure")
    record(store, cases[2], InputAccess.SEMANTIC, status="ERROR", reason="parse failed")
    record(store, cases[3], InputAccess.SEMANTIC, status="NOT_APPLICABLE", reason="needs a model")
    g = build_report(cases, store, "cand", "r1").overall["SUPPORT"]  # cases[4] is never recorded
    assert (g.judged, g.abstain, g.error, g.not_applicable, g.not_recorded) == (1, 1, 1, 1, 1)
    assert g.unjudged_reasons == {"ABSTAIN: unsure": 1, "ERROR: parse failed": 1,
                                  "NOT_APPLICABLE: needs a model": 1}
    assert g.coverage.numerator == 1 and g.recall.denominator == 5


def test_evidence_dispositions_and_reasons_are_counted_when_reported(tmp_path):
    store = RecordingStore(tmp_path)
    cases = [reviewed_case("SUPPORT", f"s{n}", expected="SUPPORTED") for n in range(3)]
    dropped = {"evidence_disposition": "DROPPED_LITERAL_CHECK", "evidence_reason": "value not in Passage"}
    record(store, cases[0], InputAccess.SEMANTIC, status="NOT_APPLICABLE", diagnostics=dropped)
    record(store, cases[1], InputAccess.SEMANTIC, status="NOT_APPLICABLE", diagnostics=dropped)
    record(store, cases[2], InputAccess.SEMANTIC, status="NOT_APPLICABLE")  # reports none
    g = build_report(cases, store, "cand", "r1").overall["SUPPORT"]
    assert g.dispositions == {"DROPPED_LITERAL_CHECK": 2}
    assert g.disposition_reasons == {"value not in Passage": 2}


def test_the_report_says_false_high_confidence_and_budget_cuts_are_not_evaluated(tmp_path):
    case = reviewed_case("INDEPENDENCE", "i1")
    store = RecordingStore(tmp_path)
    record(store, case, InputAccess.SEMANTIC, label="INDEPENDENT")
    report = build_report([case], store, "cand", "r1")
    text = render_text(report)
    assert "False-HIGH Confidence is not evaluated" in text
    assert "Budget-cut Evidence is not evaluated" in text
    assert len(report.not_evaluated) == 2


# ---- the baseline through the CLI ------------------------------------------------------------

def baseline_cases(tmp_path):
    return write_jsonl(tmp_path / "cases.jsonl", [
        {**reviewed(), "id": "p1", "type": "PAIR", "primary_category": "numbers",
         "origin": "synthetic", "inputs": PAIR_INPUTS, "expected": "SAME_FACT", "rationale": "r"},
        {**reviewed(), "id": "i1", "type": "INDEPENDENCE", "primary_category": "sources",
         "origin": "synthetic", "expected": "INDEPENDENT", "rationale": "r",
         "inputs": {"source_a_url": "https://a.example/x", "source_a_text": "a",
                    "source_b_url": "https://b.test/y", "source_b_text": "b"}}])


def test_the_baseline_command_records_and_reports_the_baseline_view(tmp_path, capsys):
    path = baseline_cases(tmp_path)
    assert bench(["baseline", "--cases", str(path), "--recordings", str(tmp_path / "rec"),
                  "--run", "r1"]) == 0
    out = capsys.readouterr().out
    assert "candidate=deterministic-baseline" in out and "input_access=BASELINE" in out
    # The same recordings reported as a SEMANTIC view are stale, not silently accepted.
    assert bench(["report", "--cases", str(path), "--recordings", str(tmp_path / "rec"),
                  "--candidate", b.CANDIDATE, "--run", "r1", "--input-access", "SEMANTIC"]) == 0
    assert "  PAIR: eligible=1 judged=0 abstain=0 error=0 not_applicable=0 not_recorded=0 stale=1" in capsys.readouterr().out
    assert bench(["report", "--cases", str(path), "--recordings", str(tmp_path / "rec"),
                  "--candidate", b.CANDIDATE, "--run", "r1", "--input-access", "BASELINE"]) == 0
    assert "  PAIR: eligible=1 judged=1 abstain=0 error=0 not_applicable=0 not_recorded=0 stale=0" in capsys.readouterr().out


def test_a_changed_case_under_the_same_baseline_run_is_refused(tmp_path, capsys):
    path = baseline_cases(tmp_path)
    args = ["baseline", "--cases", str(path), "--recordings", str(tmp_path / "rec"), "--run", "r1"]
    assert bench(args) == 0
    capsys.readouterr()
    edited = path.read_text().replace("https://b.test/y", "https://c.test/y")
    path.write_text(edited)
    assert bench(args) == 1
    assert "case changed" in capsys.readouterr().out


def test_every_judged_baseline_label_belongs_to_its_case_type(tmp_path):
    path = baseline_cases(tmp_path)
    store = RecordingStore(tmp_path / "rec")
    from insightforge_agent.benchmark.cases import load_cases
    verdicts = b.run_baseline(load_cases(path), store, "r1")
    assert {v.status for v in verdicts} == {JudgeStatus.JUDGED}
