"""T20 B6: retention is unchanged by the Sub-task lifecycle. An expired Run loses its plan and
lifecycle events with the rest of its detail, and keeps its stage trail and how it ended, so the
Run page can show its stage history and say that its Sub-task detail is no longer available."""

import pytest

from insightforge_agent.domain.models import RunEvent
from tests.stores.retention_world import OLD, World, item, ref

LIFECYCLE = {
    "plan_accepted": {"sub_tasks": [{"id": "s1", "aspect": "A", "query": "q",
                                     "source_type": "web"}]},
    "subtask_started": {"subtask_id": "s1", "phase": "research"},
    "subtask_finished": {"subtask_id": "s1", "outcome": "succeeded"},
}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "app.db")
    yield w
    w.repos.close()


def lifecycle(world: World, run_id: str) -> None:
    for type_, payload in LIFECYCLE.items():
        world.repos.events.append(RunEvent(owner_id="alice", run_id=run_id, type=type_,
                                           payload=payload, created_at=OLD))


def test_an_expired_runs_lifecycle_detail_goes_and_its_stage_trail_stays(world):
    world.web("r1", "obs_cited", paragraphs=1)
    world.complete("r1", evidence=[item("ev1", ref("obs_cited"))])
    world.operational("r1")
    lifecycle(world, "r1")
    world.sweep()
    kept = {t for (t,) in world.sql("SELECT type FROM run_events WHERE run_id='r1'")}
    assert kept == {"stage", "run_failed"}


def test_an_unexpired_runs_lifecycle_is_kept(world):
    world.run("r2")  # not terminal: never a candidate
    lifecycle(world, "r2")
    world.sweep()
    kept = {t for (t,) in world.sql("SELECT type FROM run_events WHERE run_id='r2'")}
    assert kept == set(LIFECYCLE)
