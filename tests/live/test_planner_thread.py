"""Live spike: criteria 1-4 from docs/design.md section 12. Run with `pytest -m live`."""

import json
import time

import pytest

from tests.live.planner_spike import run_spike

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def outcome():
    return run_spike()


def test_planning_produces_plan_and_graph_advances(outcome):
    assert outcome.plan is not None
    assert 3 <= len(outcome.plan.sub_tasks) <= 6


def test_researching_resumes_same_thread_one_task_per_subtask(outcome):
    assert outcome.plan_survived_resume
    ids = {t.id for t in outcome.plan.sub_tasks}
    assert sum(outcome.research_turn_task_counts) == len(ids)


def test_every_subtask_gets_result_and_dropped_one_is_repaired(outcome):
    ids = {t.id for t in outcome.plan.sub_tasks}
    assert len(outcome.missing_after_research) == 1  # the deliberately dropped one
    assert set(outcome.final_results) == ids


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
