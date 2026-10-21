"""Live spike: criteria 1-4 from docs/design.md section 12. Run with `pytest -m live`."""

import json
import time

import pytest

from tests.live.planner_spike import run_spike
from insightforge_agent.pipeline.dispatch import check_dispatches

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def outcome():
    return run_spike()


def test_planning_produces_plan_and_graph_advances(outcome):
    assert outcome.plan is not None
    assert 3 <= len(outcome.plan.sub_tasks) <= 6


def test_researching_uses_the_approved_plan_from_the_thread(outcome):
    ids = [t.id for t in outcome.plan.sub_tasks]
    assert outcome.thread_plan == outcome.plan
    # The RESEARCH message carries no plan text, yet every dispatch is one-to-one
    # with the approved Sub-tasks and carries each approved query verbatim.
    for t in outcome.plan.sub_tasks:
        assert t.query not in outcome.research_prompt
    check_dispatches(outcome.plan, outcome.research, ids)


def test_dropped_subtask_gets_a_fresh_result_from_the_repair_dispatch(outcome):
    ids = [t.id for t in outcome.plan.sub_tasks]
    (dropped,) = outcome.discarded
    assert {i for i, o in outcome.before_repair.items() if o.status == "missing"} == {dropped}
    # Repair dispatches only the missing ID with its original scope.
    check_dispatches(outcome.plan, outcome.repair, [dropped])
    fixed = outcome.after_repair[dropped]
    assert (fixed.status, fixed.phase) == ("succeeded", "REPAIR")
    original_ids = {d.tool_call_id for d in outcome.research}
    assert fixed.dispatch.tool_call_id not in original_ids
    # Everything else keeps its original RESEARCH result.
    for sid in ids:
        if sid != dropped:
            assert outcome.after_repair[sid].dispatch.tool_call_id == outcome.before_repair[sid].dispatch.tool_call_id


def _run_with_rate_limit_retry(attempts: int = 4):
    """Free-tier providers cap tokens per minute; wait and retry rather than fail the measurement."""
    for attempt in range(attempts):
        try:
            return run_spike()
        except Exception as e:  # noqa: BLE001 - provider-specific RateLimitError types
            if "rate" not in type(e).__name__.lower() or attempt == attempts - 1:
                raise
            time.sleep(60)


def test_parallel_dispatch_share_over_5_runs():
    """Measured, not required: prints how many runs sent every `task` in one turn."""
    one_turn = 0
    for _ in range(5):
        o = _run_with_rate_limit_retry()
        counts = o.research_turn_task_counts
        one_turn += counts == [len(o.plan.sub_tasks)]
    print("PARALLEL_SHARE", json.dumps({"runs": 5, "all_task_calls_in_one_turn": one_turn}))
