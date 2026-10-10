import json

import pytest
from pydantic import ValidationError

from insightforge_agent.benchmark.cases import PredictionCase
from insightforge_agent.benchmark.judge import Judge, JudgeStatus, Usage, Verdict
from insightforge_agent.benchmark.recording import (
    REDACTED,
    ConflictingRecording,
    RecordingError,
    RecordingStore,
    ReplayJudge,
    ReplayMiss,
    RunIdentity,
    redact_text,
    sanitize_config,
)

from .helpers import make_case


def verdict(case, **kw):
    base = {"case_id": case.id, "case_type": case.type, "status": "JUDGED", "label": "SAME_FACT"}
    base.update(kw)
    return Verdict.model_validate(base)


def identity(**kw):
    base = dict(candidate="det", candidate_version="1", model="scripted", run="r1",
                config={"temperature": 0})
    base.update(kw)
    return RunIdentity(**base)


# ---- prediction-facing case -------------------------------------------------------------------

def test_a_judge_never_sees_the_expected_label_rationale_or_review_data():
    case = make_case(rationale="SECRET RATIONALE", reviewer=None)
    view = case.prediction_view()
    assert set(type(view).model_fields) == {"id", "type", "inputs"}
    dumped = json.dumps(view.model_dump(mode="json"))
    assert "SECRET RATIONALE" not in dumped
    assert "expected" not in dumped and "SAME_FACT" not in dumped
    assert "primary_category" not in dumped and "critical" not in dumped


def test_a_judge_is_a_plain_protocol_taking_a_prediction_case():
    class Echo:
        def judge(self, case: PredictionCase) -> Verdict:
            return verdict(case)

    judge: Judge = Echo()
    case = make_case()
    assert judge.judge(case.prediction_view()).label == "SAME_FACT"


# ---- verdicts ---------------------------------------------------------------------------------

def test_only_a_judged_verdict_carries_a_label_of_the_right_type():
    case = make_case()
    assert verdict(case).status is JudgeStatus.JUDGED
    with pytest.raises(ValidationError):
        verdict(case, label="SUPPORTED")          # wrong case type
    with pytest.raises(ValidationError):
        verdict(case, label=None)                 # JUDGED needs a label
    for status in ("ABSTAIN", "ERROR", "NOT_APPLICABLE"):
        with pytest.raises(ValidationError):
            verdict(case, status=status, reason="why")  # label still set
        ok = verdict(case, status=status, label=None, reason="why")
        assert ok.label is None and ok.status.value == status
        with pytest.raises(ValidationError):
            verdict(case, status=status, label=None)    # reason required


def test_the_three_non_judgments_are_distinct_statuses():
    assert {s.value for s in JudgeStatus} == {"JUDGED", "ABSTAIN", "ERROR", "NOT_APPLICABLE"}


def test_grounding_failure_is_a_diagnostic_on_not_grounded_only():
    case = make_case("GROUND", expected="NOT_GROUNDED")
    ok = verdict(case, label="NOT_GROUNDED", grounding_failure="UNSUPPORTED_ENTITY")
    assert ok.grounding_failure.value == "UNSUPPORTED_ENTITY"
    with pytest.raises(ValidationError):
        verdict(case, label="GROUNDED", grounding_failure="UNSUPPORTED_ENTITY")


def test_missing_usage_is_unknown_never_zero():
    usage = verdict(make_case()).usage
    assert usage.input_tokens is None and usage.output_tokens is None
    assert usage.latency_ms is None and usage.retries is None and usage.parse_failures is None
    assert usage.tokens_known is False
    assert Usage(input_tokens=0, output_tokens=0).tokens_known is True


def test_scores_are_diagnostics_and_nothing_in_a_verdict_is_a_confidence():
    case = make_case()
    v = verdict(case, model_scores={"SAME_FACT": 0.93})
    assert v.model_scores == {"SAME_FACT": 0.93}
    assert not any("confidence" in name.lower() for name in Verdict.model_fields)
    with pytest.raises(ValidationError):
        verdict(case, model_scores={"x": float("nan")})
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1)


# ---- recording --------------------------------------------------------------------------------

def test_normalised_verdicts_and_raw_replies_are_stored_separately(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case().prediction_view()
    store.record(identity(), case, verdict(case), raw_reply='{"answer": "same"}')
    verdicts = (tmp_path / "verdicts.jsonl").read_text()
    raw = (tmp_path / "raw.jsonl").read_text()
    assert "SAME_FACT" in verdicts and "answer" not in verdicts
    assert "answer" in raw and "SAME_FACT" not in raw


def test_a_record_names_candidate_version_model_config_case_and_run(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case().prediction_view()
    rec = store.record(identity(config={"temperature": 0, "prompt_hash": "abc"}), case, verdict(case))
    assert (rec.candidate, rec.candidate_version, rec.model, rec.case_id, rec.run) == (
        "det", "1", "scripted", "c001", "r1")
    assert rec.config == {"temperature": 0, "prompt_hash": "abc"} and len(rec.config_hash) == 64
    assert len(rec.case_hash) == 64


def test_ground_truth_never_reaches_the_store(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case(rationale="because of reasons", reviewer=None)
    store.record(identity(), case.prediction_view(), verdict(case))
    text = (tmp_path / "verdicts.jsonl").read_text()
    for forbidden in ("expected", "rationale", "because of reasons", "label_status", "reviewer"):
        assert forbidden not in text


def test_replay_reproduces_the_same_normalised_result_after_reopening(tmp_path):
    case = make_case().prediction_view()
    original = verdict(case, model_scores={"SAME_FACT": 0.9}, usage=Usage(input_tokens=3, output_tokens=1))
    RecordingStore(tmp_path).record(identity(), case, original)
    replay = ReplayJudge(RecordingStore(tmp_path), "det", "r1")
    first, second = replay.judge(case), replay.judge(case)
    assert first == second == original


def test_replay_of_a_missing_or_changed_case_fails(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case().prediction_view()
    store.record(identity(), case, verdict(case))
    with pytest.raises(ReplayMiss):
        ReplayJudge(store, "det", "other-run").judge(case)
    changed = make_case(inputs={"statement_a": "x", "statement_b": "y"}).prediction_view()
    with pytest.raises(ReplayMiss):
        ReplayJudge(store, "det", "r1").judge(changed)


def test_identical_re_recording_is_idempotent_and_conflicts_fail(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case().prediction_view()
    store.record(identity(), case, verdict(case), raw_reply="raw")
    store.record(identity(), case, verdict(case), raw_reply="raw")
    assert len((tmp_path / "verdicts.jsonl").read_text().splitlines()) == 1
    with pytest.raises(ConflictingRecording):
        store.record(identity(), case, verdict(case, label="DIFFERENT_FACT"))
    with pytest.raises(ConflictingRecording):
        store.record(identity(candidate_version="2"), case, verdict(case))   # version is explicit
    with pytest.raises(ConflictingRecording):
        store.record(identity(), case, verdict(case), raw_reply="other raw")
    assert store.get("det", "c001", "r1").verdict.label == "SAME_FACT"


def test_the_same_case_under_another_run_or_candidate_is_a_separate_record(tmp_path):
    store = RecordingStore(tmp_path)
    case = make_case().prediction_view()
    store.record(identity(run="r1"), case, verdict(case))
    store.record(identity(run="r2"), case, verdict(case, label="DIFFERENT_FACT"))
    store.record(identity(candidate="llm"), case, verdict(case))
    assert len(store.records_for("det", "r1")) == 1 and len(store.records_for("det", "r2")) == 1


def test_a_verdict_for_another_case_is_refused(tmp_path):
    store = RecordingStore(tmp_path)
    a, b = make_case(case_id="a").prediction_view(), make_case(case_id="b").prediction_view()
    with pytest.raises(RecordingError):
        store.record(identity(), a, verdict(b))


def test_a_corrupt_store_fails_to_open(tmp_path):
    (tmp_path / "verdicts.jsonl").write_text("not json\n")
    with pytest.raises(RecordingError):
        RecordingStore(tmp_path)


# ---- secrets ----------------------------------------------------------------------------------

def test_secrets_are_redacted_from_text():
    text = ("key sk-ant-api03-ABCDEFGHIJKLMNOP and Authorization: Bearer abcdef123456789 "
            "and api_key=hunter2hunter2 plus mine-123")
    cleaned = redact_text(text, known_secrets=("mine-123",))
    for leaked in ("ABCDEFGHIJKLMNOP", "abcdef123456789", "hunter2hunter2", "mine-123"):
        assert leaked not in cleaned
    assert REDACTED in cleaned


def test_configuration_keeps_only_explicit_safe_fields():
    safe, dropped = sanitize_config(
        {"temperature": 0, "api_key": "sk-live-ABCDEFGHIJKLMNOP", "headers": {"Authorization": "x"},
         "prompt_hash": "deadbeef"}
    )
    assert safe == {"temperature": 0, "prompt_hash": "deadbeef"}
    assert dropped == ["api_key", "headers"]


def test_nothing_secret_is_persisted(tmp_path):
    store = RecordingStore(tmp_path, known_secrets=("topsecretvalue",))
    case = make_case().prediction_view()
    v = verdict(case, diagnostics={"note": "used topsecretvalue", "api_key": "sk-ABCDEFGHIJKLMNOPQRS"})
    store.record(
        identity(config={"temperature": 0, "api_key": "topsecretvalue"}), case, v,
        raw_reply="HTTP header Authorization: Bearer abcdefghijklmnop echo topsecretvalue",
    )
    on_disk = (tmp_path / "verdicts.jsonl").read_text() + (tmp_path / "raw.jsonl").read_text()
    for leaked in ("topsecretvalue", "abcdefghijklmnop", "sk-ABCDEFGHIJKLMNOPQRS"):
        assert leaked not in on_disk
