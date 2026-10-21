"""Regression tests for the PR #38 review corrections: stale recordings, fail-closed ledger
writes, cost overruns, credential redaction, bootstrap label alignment."""

from decimal import Decimal

import pytest

from insightforge_agent.benchmark.cli import main as bench
from insightforge_agent.benchmark.costs import (
    BudgetGuard,
    Ledger,
    LedgerError,
    UnresolvedReservation,
    run_sequential,
)
from insightforge_agent.benchmark.judge import Usage, Verdict
from insightforge_agent.benchmark.recording import (
    REDACTED,
    RecordingStore,
    RunIdentity,
    StaleRecording,
    redact_text,
)
from insightforge_agent.benchmark.report import MixedCandidateVersions, build_report
from insightforge_agent.benchmark.stats import Outcome, paired_bootstrap_f1

from .helpers import case_dict, make_case, reviewed, write_jsonl
from .test_costs import BOUND, PRICING, WORST, ScriptedJudge, auth, cases, identity, make_guard, run

OK = Usage(input_tokens=100, output_tokens=50)


def recorded(store, case, version="1", model="m", config=None, label="SAME_FACT", run_id="r1"):
    view = case.prediction_view()
    store.record(
        RunIdentity(candidate="cand", candidate_version=version, model=model, run=run_id,
                    config=config or {}),
        view, Verdict(case_id=view.id, case_type=view.type, status="JUDGED", label=label, usage=Usage()),
    )


# ---- stale recordings: resume --------------------------------------------------------------------

def test_resume_refuses_a_recording_whose_case_changed_and_makes_no_call(tmp_path):
    run(tmp_path, [OK], 1)
    guard = make_guard(tmp_path)
    changed = [make_case(case_id="k0", inputs={"statement_a": "new", "statement_b": "text"})
               .prediction_view()]
    judge = ScriptedJudge([OK], guard)
    with pytest.raises(StaleRecording, match="k0.*case changed"):
        run_sequential(cases=changed, judge=judge, guard=guard, bound_for=lambda c: BOUND,
                       store=RecordingStore(tmp_path / "rec"), identity=identity())
    assert judge.calls == 0 and guard.ledger.committed == Decimal("0.0002")


@pytest.mark.parametrize("changes,reason", [
    ({"candidate_version": "2"}, "version"),
    ({"model": "other-model"}, "model"),
    ({"config": {"temperature": 1}}, "configuration"),
])
def test_resume_refuses_a_recording_from_another_version_model_or_configuration(tmp_path, changes, reason):
    run(tmp_path, [OK], 1)
    guard = make_guard(tmp_path)
    judge = ScriptedJudge([OK], guard)
    other = identity().model_copy(update=changes)
    with pytest.raises(StaleRecording, match=reason):
        run_sequential(cases=cases(1), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                       store=RecordingStore(tmp_path / "rec"), identity=other)
    assert judge.calls == 0


def test_resume_still_skips_a_current_recording(tmp_path):
    run(tmp_path, [OK], 1)
    guard = make_guard(tmp_path)
    judge = ScriptedJudge([], guard)
    summary = run_sequential(cases=cases(1), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                             store=RecordingStore(tmp_path / "rec"), identity=identity())
    assert summary.skipped_recorded == ["k0"] and judge.calls == 0


# ---- stale recordings and mixed versions: reporting --------------------------------------------

def reviewed_pair(case_id="p1", **kw):
    return make_case("PAIR", case_id, **{**reviewed(), **kw})


def test_a_report_never_uses_a_recording_made_for_a_changed_case(tmp_path):
    store = RecordingStore(tmp_path)
    original = reviewed_pair()
    recorded(store, original, label="SAME_FACT")
    edited = reviewed_pair(inputs={"statement_a": "changed", "statement_b": "inputs"})
    report = build_report([edited], store, "cand", "r1")
    g = report.overall["PAIR"]
    assert g.stale == 1 and g.judged == 0 and g.coverage.numerator == 0
    assert g.recall.numerator == 0 and g.recall.denominator == 1  # counted as a miss, not a hit
    assert any("stale" in n for n in report.notices)
    fresh = build_report([original], store, "cand", "r1").overall["PAIR"]
    assert fresh.stale == 0 and fresh.judged == 1


def test_different_candidate_versions_are_never_aggregated(tmp_path):
    store = RecordingStore(tmp_path)
    a, b = reviewed_pair("p1"), reviewed_pair("p2")
    recorded(store, a, version="1")
    recorded(store, b, version="2")
    with pytest.raises(MixedCandidateVersions, match="mixes 2"):
        build_report([a, b], store, "cand", "r1")
    with pytest.raises(MixedCandidateVersions):
        build_report([a, b], store, "cand", "r1", allow_proposed=True)  # exploratory too


def test_a_version_filter_reports_one_version_and_says_what_it_left_out(tmp_path):
    store = RecordingStore(tmp_path)
    a, b = reviewed_pair("p1"), reviewed_pair("p2")
    recorded(store, a, version="1")
    recorded(store, b, version="2")
    report = build_report([a, b], store, "cand", "r1", candidate_version="2")
    assert report.candidate_version == "2"
    assert report.overall["PAIR"].judged == 1 and report.overall["PAIR"].not_recorded == 1
    assert any("other candidate versions" in n for n in report.notices)


def test_same_version_with_a_different_configuration_is_also_refused(tmp_path):
    store = RecordingStore(tmp_path)
    a, b = reviewed_pair("p1"), reviewed_pair("p2")
    recorded(store, a, config={"temperature": 0})
    recorded(store, b, config={"temperature": 1})
    with pytest.raises(MixedCandidateVersions):
        build_report([a, b], store, "cand", "r1", candidate_version="1")


def test_the_cli_refuses_mixed_versions_unless_one_is_selected(tmp_path, capsys):
    store = RecordingStore(tmp_path / "rec")
    a, b = reviewed_pair("p1"), reviewed_pair("p2")
    recorded(store, a, version="1")
    recorded(store, b, version="2")
    path = write_jsonl(tmp_path / "c.jsonl", [case_dict(case_id="p1", **reviewed()),
                                              case_dict(case_id="p2", **reviewed())])
    args = ["report", "--cases", str(path), "--recordings", str(tmp_path / "rec"),
            "--candidate", "cand", "--run", "r1"]
    assert bench(args) == 1 and "mixes 2" in capsys.readouterr().out
    assert bench([*args, "--candidate-version", "2"]) == 0
    assert "version=2" in capsys.readouterr().out


# ---- ledger persistence failures ---------------------------------------------------------------

def failing_append(monkeypatch, fail_on_call):
    calls = {"n": 0}
    real = Ledger._append

    def append(self, line):
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise OSError("disk full")
        real(self, line)

    monkeypatch.setattr(Ledger, "_append", append)


def test_a_failed_reservation_write_stops_the_run_before_any_call(tmp_path, monkeypatch):
    guard = make_guard(tmp_path)
    judge = ScriptedJudge([OK], guard)
    failing_append(monkeypatch, 1)
    with pytest.raises(LedgerError, match="could not persist"):
        run_sequential(cases=cases(1), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                       store=RecordingStore(tmp_path / "rec"), identity=identity())
    assert judge.calls == 0
    with pytest.raises(LedgerError, match="unusable"):  # the poisoned ledger refuses everything
        guard.reserve({"candidate": "c", "case_id": "x", "run": "r"}, BOUND)


def test_a_failed_settlement_write_keeps_the_result_stops_and_fails_closed_on_reopen(tmp_path, monkeypatch):
    guard = make_guard(tmp_path)
    store = RecordingStore(tmp_path / "rec")
    judge = ScriptedJudge([OK, OK], guard)
    failing_append(monkeypatch, 2)  # call 1 reserves k0, call 2 is its settlement
    summary = run_sequential(cases=cases(2), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                             store=store, identity=identity())
    assert summary.completed == ["k0"] and judge.calls == 1
    assert "ledger write failed" in summary.halted_reason
    assert store.get("fake", "k0", "r1") is not None and store.get("fake", "k1", "r1") is None
    with pytest.raises(LedgerError):
        guard.ledger.committed  # noqa: B018 - still readable
        guard.reserve({"candidate": "c", "case_id": "x", "run": "r"}, BOUND)
    monkeypatch.undo()
    with pytest.raises(UnresolvedReservation):  # the disk holds an unsettled reservation
        Ledger.open(tmp_path / "ledger.jsonl")
    assert Ledger.open(tmp_path / "ledger.jsonl", recover_interrupted=True).committed == WORST


# ---- overrun beyond the reserved bound ---------------------------------------------------------

def test_execution_stops_when_actual_cost_exceeds_the_reservation_even_with_headroom(tmp_path):
    over = Usage(input_tokens=5000, output_tokens=5000)  # 0.015 against a 0.002 reservation
    summary, guard, store, judge = run(tmp_path, [over, OK, OK], 3, ceiling="100")
    assert guard.remaining > Decimal("99")  # plenty of overall budget left
    assert summary.completed == ["k0"] and judge.calls == 1
    assert "exceeded the reserved worst case" in summary.halted_reason
    assert store.get("fake", "k0", "r1") is not None and store.get("fake", "k1", "r1") is None
    assert guard.ledger.committed == Decimal("0.015")  # the true cost is still recorded


def test_a_call_within_its_reservation_does_not_stop_the_run(tmp_path):
    summary, _, _, judge = run(tmp_path, [OK, OK], 2, ceiling="100")
    assert summary.halted_reason is None and judge.calls == 2


# ---- credential redaction ----------------------------------------------------------------------

@pytest.mark.parametrize("raw,secret", [
    ('{"api_key": "sk-live-abc123def456ghi789", "answer": "same"}', "sk-live-abc123def456ghi789"),
    ('{"api_key":"short","answer":"same"}', '"short"'),
    ('{"Authorization": "Basic dXNlcjpwYXNz", "answer": "same"}', "dXNlcjpwYXNz"),
    ('{"client_secret": "has spaces in it", "answer": "same"}', "has spaces in it"),
    ('{"headers": {"x-auth-token": "abc123"}, "answer": "same"}', "abc123"),
    ("{'password': 'hunter2', 'answer': 'same'}", "hunter2"),
    ('{"access_token": 98765432, "answer": "same"}', "98765432"),
    ('{\\"api_key\\": \\"escaped-secret\\", \\"answer\\": \\"same\\"}', "escaped-secret"),
])
def test_quoted_json_credential_fields_are_redacted(raw, secret):
    cleaned = redact_text(raw)
    assert secret not in cleaned and REDACTED in cleaned and "answer" in cleaned


def test_usage_counters_in_a_raw_reply_are_not_mistaken_for_credentials():
    raw = '{"usage": {"input_tokens": 12, "output_tokens": 3, "max_tokens": 50}, "total_tokens": 15}'
    assert redact_text(raw) == raw


def test_a_sensitive_raw_reply_never_reaches_the_disk(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case().prediction_view()
    raw = ('{"label": "SAME_FACT", "debug": {"api_key": "sk-ant-api03-ZZZZZZZZZZZZZZZZ", '
           '"refresh_token": "r-tok-123456", "password": "p4ss w0rd"}, "usage": {"input_tokens": 7}}')
    store.record(identity(), case,
                 Verdict(case_id=case.id, case_type=case.type, status="JUDGED", label="SAME_FACT"),
                 raw_reply=raw)
    stored = (tmp_path / "raw.jsonl").read_text()
    for leaked in ("ZZZZZZZZZZZZZZZZ", "r-tok-123456", "p4ss w0rd"):
        assert leaked not in stored
    assert "SAME_FACT" in stored and "input_tokens" in stored


# ---- bootstrap label alignment -----------------------------------------------------------------

def rows(expected_positive=True, label="SAME_FACT", n=5):
    return {f"c{i}": Outcome(expected_positive, True, expected_label=label) for i in range(n)}


def test_paired_bootstrap_requires_the_same_expected_labels_for_every_candidate():
    assert paired_bootstrap_f1({"a": rows(), "b": rows()}).differences  # aligned: fine
    misaligned = rows()
    misaligned["c2"] = Outcome(False, True, expected_label="DIFFERENT_FACT")
    with pytest.raises(ValueError, match="expected labels differ for case 'c2'"):
        paired_bootstrap_f1({"a": rows(), "b": misaligned})


def test_a_different_negative_label_behind_the_same_positivity_is_still_a_mismatch():
    a = {"c0": Outcome(False, False, expected_label="DIFFERENT_FACT")}
    b = {"c0": Outcome(False, False, expected_label="CONTRADICTORY")}
    with pytest.raises(ValueError, match="expected labels differ"):
        paired_bootstrap_f1({"a": a, "b": b})
