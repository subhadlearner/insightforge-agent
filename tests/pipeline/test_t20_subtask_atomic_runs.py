"""T20 B6 review: a Run whose plan or outcome event cannot be stored also stores no plan or
outcome, so the Run page and the Sub-task rows never disagree (the repository side is
tests/stores/test_t20_subtask_atomic.py, which injects the failure the same way)."""

import pytest

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState, SubTaskStatus
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.pipeline.test_happy_path import BRIEF
from tests.stores.test_t20_subtask_atomic import abort_events


@pytest.fixture
def sqlite_deps(make_deps):
    made = []

    def make(**overrides):
        repos = SqliteRepos(":memory:")
        made.append(repos)
        return make_deps(repos=repos,
                         store=SourceStore(repos.sources, repos.observations, repos.passages),
                         **overrides)
    yield make
    for r in made:
        r.close()


def test_a_run_whose_outcome_event_fails_stores_no_outcome_it_never_logged(sqlite_deps):
    deps = sqlite_deps()
    abort_events(deps.repos, "subtask_finished")
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    events = deps.repos.events.read_after("alice", run.id, 0, 10_000)
    logged = {e.payload["subtask_id"] for e in events if e.type == "subtask_finished"}
    final = {r.subtask.id for r in deps.repos.subtasks.list("alice", run.id)
             if r.status != SubTaskStatus.PENDING}
    assert final == logged == set()  # the page and the rows agree: no outcome either way


def test_a_run_whose_plan_event_fails_stores_no_plan_it_never_logged(sqlite_deps):
    deps = sqlite_deps()
    abort_events(deps.repos, "plan_accepted")
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert deps.repos.subtasks.list("alice", run.id) == []
    assert "plan_accepted" not in [e.type for e in deps.repos.events.read_after("alice", run.id)]


def test_a_completed_runs_rows_match_its_logged_outcomes_exactly(sqlite_deps):
    deps = sqlite_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    events = deps.repos.events.read_after("alice", run.id, 0, 10_000)
    logged = {e.payload["subtask_id"]: e.payload["outcome"] for e in events
              if e.type == "subtask_finished"}
    rows = {r.subtask.id: r.status.value.lower()
            for r in deps.repos.subtasks.list("alice", run.id)}
    assert logged == rows and len(rows) == 3
