import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from insightforge_agent import main as entry_main
from insightforge_agent.benchmark.cases import load_cases
from insightforge_agent.benchmark.cli import main as bench
from insightforge_agent.benchmark.costs import Ledger
from insightforge_agent.benchmark.judge import Usage, Verdict
from insightforge_agent.benchmark.recording import RecordingStore, RunIdentity
from insightforge_agent.benchmark.report import build_report, render_text, to_dict

from .helpers import case_dict, make_case, reviewed, write_jsonl

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "benchmark" / "cases.jsonl"


def record(store, case, label=None, status="JUDGED", run="r1", reason="because"):
    view = case.prediction_view()
    kw = {"label": label} if status == "JUDGED" else {"label": None, "reason": reason}
    store.record(
        RunIdentity(candidate="cand", candidate_version="7", model="m", run=run),
        view,
        Verdict(case_id=view.id, case_type=view.type, status=status, usage=Usage(), **kw),
    )


def pair(case_id, expected, **kw):
    return make_case("PAIR", case_id, expected=expected, **{**reviewed(), **kw})


# Four reviewed PAIR cases: 2 expected SAME_FACT (positives), 2 expected negatives.
def pair_cases():
    return [pair("p1", "SAME_FACT"), pair("p2", "SAME_FACT"),
            pair("n1", "DIFFERENT_FACT"), pair("n2", "CONTRADICTORY")]


def test_denominators_and_false_positives_are_exact(tmp_path):
    store = RecordingStore(tmp_path)
    cases = pair_cases()
    record(store, cases[0], "SAME_FACT")            # TP
    record(store, cases[1], "DIFFERENT_FACT")       # FN
    record(store, cases[2], "SAME_FACT")            # FP (a false merge)
    record(store, cases[3], "CONTRADICTORY")        # TN
    g = build_report(cases, store, "cand", "r1").overall["PAIR"]
    assert (g.precision.numerator, g.precision.denominator) == (1, 2)
    assert (g.recall.numerator, g.recall.denominator) == (1, 2)
    assert (g.false_positive_rate.numerator, g.false_positive_rate.denominator) == (1, 2)
    assert (g.accuracy.numerator, g.accuracy.denominator) == (2, 4)
    assert g.false_positives == 1 and g.f1 == pytest.approx(0.5)
    assert g.note is None
    assert g.coverage.value == 1.0 and g.coverage.interval is not None


def test_missing_predictions_cannot_improve_the_numbers(tmp_path):
    store = RecordingStore(tmp_path)
    cases = pair_cases()
    record(store, cases[0], "SAME_FACT")                       # TP
    record(store, cases[1], status="ABSTAIN")                  # expected positive abstained
    record(store, cases[2], status="ERROR")                    # expected negative errored
    # cases[3] never recorded
    g = build_report(cases, store, "cand", "r1").overall["PAIR"]
    assert (g.judged, g.abstain, g.error, g.not_recorded) == (1, 1, 1, 1)
    assert (g.coverage.numerator, g.coverage.denominator) == (1, 4)
    assert (g.recall.numerator, g.recall.denominator) == (1, 2)       # abstention is a miss
    assert (g.accuracy.numerator, g.accuracy.denominator) == (1, 4)   # and counts as wrong
    assert (g.false_positive_rate.numerator, g.false_positive_rate.denominator) == (0, 0)
    assert g.false_positive_rate.value is None                        # undefined, not 0
    assert (g.worst_case_fpr.numerator, g.worst_case_fpr.denominator) == (2, 2)
    assert g.fp_upper_bound is None


def test_not_applicable_is_reported_separately_and_is_not_a_prediction(tmp_path):
    store = RecordingStore(tmp_path)
    cases = pair_cases()
    record(store, cases[0], status="NOT_APPLICABLE")
    g = build_report(cases, store, "cand", "r1").overall["PAIR"]
    assert g.not_applicable == 1 and g.judged == 0
    assert g.precision.value is None and g.precision.denominator == 0


def test_zero_failures_report_a_bound_and_never_claim_safety(tmp_path):
    store = RecordingStore(tmp_path)
    cases = pair_cases()
    for case, label in zip(cases, ["SAME_FACT", "SAME_FACT", "DIFFERENT_FACT", "CONTRADICTORY"]):
        record(store, case, label)
    report = build_report(cases, store, "cand", "r1")
    g = report.overall["PAIR"]
    assert g.false_positives == 0 and g.judged_negatives == 2
    assert g.fp_upper_bound == 1 - 0.05 ** (1 / 2)
    text = render_text(report)
    assert "not proof of safety" in text
    assert all("not proof of safety" in line for line in text.splitlines() if "safe" in line.lower())


def test_groups_cover_type_category_and_expected_judgment(tmp_path):
    cases = load_cases(FIXTURE)
    store = RecordingStore(tmp_path)
    report = build_report(cases, store, "cand", "r1", allow_proposed=True)
    assert set(report.overall) == {"PAIR", "SUPPORT", "GROUND", "INDEPENDENCE"}
    assert {"PAIR/numbers", "PAIR/aliases", "SUPPORT/paraphrase"} <= set(report.by_category)
    assert {"PAIR/SAME_FACT", "GROUND/NOT_GROUNDED"} <= set(report.by_expected)
    assert report.overall["PAIR"].not_recorded == report.overall["PAIR"].eligible


def test_qualification_uses_reviewed_labels_and_counts_exclusions(tmp_path):
    cases = load_cases(FIXTURE)  # includes 1 proposed critical (fx-p4) and 1 disputed (fx-s3)
    report = build_report(cases, RecordingStore(tmp_path), "cand", "r1")
    assert report.mode == "QUALIFICATION"
    ex = report.exclusions
    assert (ex.disputed, ex.proposed_not_allowed, ex.proposed_critical_not_allowed,
            ex.proposed_included) == (1, 1, 1, 0)
    assert report.overall["PAIR"].eligible == 3
    assert "SUPPORT/INSUFFICIENT_EVIDENCE" not in report.by_expected   # only case was disputed
    text = render_text(report)
    assert "[QUALIFICATION]" in text and "--allow-proposed" in text and "EXPLORATORY" not in text


def test_allow_proposed_is_stamped_exploratory_and_disputed_stays_out(tmp_path):
    cases = load_cases(FIXTURE)
    report = build_report(cases, RecordingStore(tmp_path), "cand", "r1", allow_proposed=True)
    assert report.mode == "EXPLORATORY"
    assert report.exclusions.proposed_included == 1 and report.exclusions.disputed == 1
    assert report.overall["PAIR"].eligible == 4
    text = render_text(report)
    assert "[EXPLORATORY]" in text and "not a qualification result" in text


def test_a_report_reads_predictions_but_ground_truth_comes_only_from_the_cases(tmp_path):
    store = RecordingStore(tmp_path)
    case = pair("p1", "SAME_FACT")
    record(store, case, "DIFFERENT_FACT")
    before = (tmp_path / "verdicts.jsonl").read_text()
    build_report([case], store, "cand", "r1")
    assert (tmp_path / "verdicts.jsonl").read_text() == before
    assert build_report([case], store, "cand", "r1").overall["PAIR"].accuracy.numerator == 0


def test_the_report_carries_the_candidate_version(tmp_path):
    store = RecordingStore(tmp_path)
    record(store, pair("p1", "SAME_FACT"), "SAME_FACT")
    assert build_report([pair("p1", "SAME_FACT")], store, "cand", "r1").candidate_version == "7"


# ---- CLI --------------------------------------------------------------------------------------

def test_bench_validate_accepts_a_good_file_and_rejects_a_bad_one(tmp_path, capsys):
    assert bench(["validate", "--cases", str(FIXTURE)]) == 0
    assert "11 valid case(s)" in capsys.readouterr().out
    bad = write_jsonl(tmp_path / "bad.jsonl", [case_dict(), case_dict(expected="SUPPORTED", case_id="x")])
    assert bench(["validate", "--cases", str(bad)]) == 1
    assert "line 2" in capsys.readouterr().out
    assert bench(["validate", "--cases", str(tmp_path / "missing.jsonl")]) == 2


def test_bench_report_prints_text_and_json(tmp_path, capsys):
    store = RecordingStore(tmp_path / "rec")
    cases = load_cases(FIXTURE)
    record(store, next(c for c in cases if c.id == "fx-p1"), "SAME_FACT")
    args = ["report", "--cases", str(FIXTURE), "--recordings", str(tmp_path / "rec"),
            "--candidate", "cand", "--run", "r1"]
    assert bench(args) == 0
    assert "[QUALIFICATION]" in capsys.readouterr().out
    assert bench([*args, "--allow-proposed", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "EXPLORATORY" and data["overall"]["PAIR"]["judged"] == 1


def test_there_is_no_eval_command_or_live_path(capsys):
    with pytest.raises(SystemExit):
        bench(["eval"])
    with pytest.raises(SystemExit):
        bench(["run", "--live"])


def test_bench_ledger_shows_spend_and_says_a_budget_number_is_not_authorisation(tmp_path, capsys):
    ledger = Ledger.open(tmp_path / "ledger.jsonl")
    ledger.reserve("res-000001", {"candidate": "c", "case_id": "a", "run": "r"}, Decimal("0.5"))
    ledger.retain("res-000001", "test")
    assert bench(["ledger", "--ledger", str(tmp_path / "ledger.jsonl"), "--approved-budget", "5"]) == 0
    out = capsys.readouterr().out
    assert "0.5 USD" in out and "UNVERIFIED" in out and "not sufficient" in out
    (tmp_path / "ledger.jsonl").write_text("garbage\n")
    assert bench(["ledger", "--ledger", str(tmp_path / "ledger.jsonl")]) == 1


def test_the_installed_entry_point_dispatches_bench(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["insightforge-agent", "bench", "validate", "--cases", str(FIXTURE)])
    with pytest.raises(SystemExit) as exit_info:
        entry_main()
    assert exit_info.value.code == 0
    assert "valid case(s)" in capsys.readouterr().out


def test_bench_makes_no_network_calls(monkeypatch, tmp_path):
    import socket

    def refuse(*a, **k):
        raise AssertionError("the benchmark must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert bench(["validate", "--cases", str(FIXTURE)]) == 0


# ---- import isolation -------------------------------------------------------------------------

LINT = [str(Path(sys.executable).with_name("lint-imports"))]


def _lint_with_probe(relative: str, line: str) -> subprocess.CompletedProcess:
    probe = ROOT / "src" / "insightforge_agent" / relative
    probe.write_text(line + "\n")
    try:
        return subprocess.run(LINT, cwd=ROOT, capture_output=True, text=True)
    finally:
        probe.unlink()


def test_production_pipeline_importing_the_benchmark_breaks_the_contract():
    result = _lint_with_probe("pipeline/_probe.py", "from insightforge_agent.benchmark import cases  # noqa")
    assert result.returncode != 0 and "never imports the benchmark" in result.stdout
