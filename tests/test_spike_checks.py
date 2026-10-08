"""Deterministic rules for missing vs failed Sub-tasks and dispatch checks (ADR-0003)."""

import pytest

from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from tests.live.spike_checks import Dispatch, check_dispatches, missing_ids, resolve

PLAN = SubTaskPlan(
    aspects=[Aspect(id="a1", name="x")],
    sub_tasks=[
        SubTask(id=i, aspect_id="a1", query=f"query-{i}", source_type="web",
                time_horizon_months=12, priority=1)
        for i in ("s1", "s2", "s3")
    ],
)


def d(sid, result, call_id=None):
    return Dispatch(call_id or f"call-{sid}", sid, f"SUBTASK_ID={sid} query-{sid}", result)


def test_status_distinguishes_success_explicit_failure_and_missing():
    assert d("s1", "RESULT: ok").status == "succeeded"
    assert d("s1", "FAILED: no data").status == "failed"
    assert d("s1", None).status == "missing"
    assert d("s1", "some prose without a marker").status == "missing"


def test_missing_ids_are_those_without_any_usable_result():
    out = resolve(["s1", "s2", "s3"], [d("s1", "RESULT: a"), d("s2", None)], [])
    assert missing_ids(out) == ["s2", "s3"]


def test_explicit_failure_is_final_and_not_filled_by_repair():
    out = resolve(["s1"], [d("s1", "FAILED: no data")], [d("s1", "RESULT: late", "repair-1")])
    assert out["s1"].status == "failed"
    assert out["s1"].phase == "RESEARCH"
    assert missing_ids(out) == []


def test_late_or_duplicate_result_never_overwrites_a_success():
    first, dup = d("s1", "RESULT: first", "c1"), d("s1", "RESULT: second", "c2")
    out = resolve(["s1"], [first], [dup])
    assert out["s1"].dispatch.tool_call_id == "c1"
    out = resolve(["s1"], [dup, first], [])
    assert out["s1"].dispatch.tool_call_id == "c2"  # first success in order wins


def test_repair_fills_only_ids_with_no_usable_result_and_records_phase():
    research = [d("s1", "RESULT: a"), d("s2", None)]
    repair = [d("s2", "RESULT: fresh", "repair-2")]
    out = resolve(["s1", "s2"], research, repair)
    assert (out["s1"].phase, out["s2"].phase) == ("RESEARCH", "REPAIR")
    assert out["s2"].dispatch.tool_call_id == "repair-2"


def test_discarded_research_result_is_not_credited_from_history():
    # The original s3 result is in the thread, but treated as never arrived.
    research = [d("s3", "RESULT: original", "orig")]
    assert resolve(["s3"], research, [], frozenset({"s3"}))["s3"].status == "missing"
    out = resolve(["s3"], research, [d("s3", "RESULT: fresh", "fresh")], frozenset({"s3"}))
    assert out["s3"].dispatch.tool_call_id == "fresh"


def test_check_dispatches_accepts_one_to_one_with_approved_scope():
    check_dispatches(PLAN, [d(i, "RESULT: x") for i in ("s1", "s2", "s3")], ["s1", "s2", "s3"])


@pytest.mark.parametrize("ds,why", [
    ([d("s1", "RESULT: x"), d("s2", "RESULT: x")], "a Sub-task was not dispatched"),
    ([d(i, "RESULT: x") for i in ("s1", "s2", "s3")] + [d("s1", "RESULT: x", "dup")], "duplicate"),
    ([d(i, "RESULT: x") for i in ("s1", "s2")] + [d("s9", "RESULT: x")], "invented ID"),
])
def test_check_dispatches_rejects_wrong_id_sets(ds, why):
    with pytest.raises(AssertionError):
        check_dispatches(PLAN, ds, ["s1", "s2", "s3"])


def test_check_dispatches_rejects_changed_scope():
    drifted = Dispatch("c", "s1", "SUBTASK_ID=s1 something else entirely", "RESULT: x")
    with pytest.raises(AssertionError, match="approved scope"):
        check_dispatches(PLAN, [drifted], ["s1"])
