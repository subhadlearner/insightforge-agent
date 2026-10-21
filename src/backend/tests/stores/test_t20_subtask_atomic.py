"""T20 B6 review: a Sub-task's stored state and the Run-log event the Run page reads from it are
written together or not at all. `save_plan` with its `plan_accepted` and `set_status` with its
`subtask_finished` are one SQLite transaction (one in-process step in memory), so no failure
between them can leave a stored plan or outcome the page never hears of, or the reverse.

The failure is injected inside the transaction with a real SQLite trigger that aborts the event
insert, which is what a crash before COMMIT leaves: SQLite rolls the transaction back."""

from datetime import UTC, datetime

import pytest

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import RunEvent, SubTaskRecord, SubTaskStatus
from insightforge_agent.domain.plan import SubTask
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.sqlite import SqliteRepos

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def task(sid: str) -> SubTask:
    return SubTask(id=sid, aspect_id="a1", query=f"q {sid}", source_type="web",
                   time_horizon_months=12, priority=1)


def event(type_: str, **payload) -> RunEvent:
    return RunEvent(owner_id="alice", run_id="r1", type=type_, payload=payload, created_at=T0)


def abort_events(repos: SqliteRepos, type_: str) -> None:
    """Every insert of a `type_` event fails inside its transaction."""
    with repos.lock, repos._db:
        repos._db.execute(
            f"CREATE TRIGGER abort_{type_} BEFORE INSERT ON run_events WHEN NEW.type='{type_}' "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")


def stored(repos, type_: str) -> list[dict]:
    return [e.payload for e in repos.events.read_after("alice", "r1", 0, 10_000) if e.type == type_]


def statuses(repos) -> dict[str, SubTaskStatus]:
    return {r.subtask.id: r.status for r in repos.subtasks.list("alice", "r1")}


@pytest.fixture(params=["sqlite", "memory"])
def repos(request):
    r = SqliteRepos(":memory:") if request.param == "sqlite" else MemoryRepos()
    yield r
    if request.param == "sqlite":
        r.close()


def fail_events(repos, monkeypatch, type_: str) -> None:
    if isinstance(repos, SqliteRepos):
        abort_events(repos, type_)
        return
    real = repos.events.append

    def append(e):
        if e.type == type_:
            raise RuntimeError("injected failure")
        return real(e)
    monkeypatch.setattr(repos.events, "append", append)


# --- the repositories ----------------------------------------------------------------------

def test_a_plan_and_its_event_are_stored_together(repos):
    repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))],
                             event=event("plan_accepted", sub_tasks=[{"id": "s1"}]))
    assert statuses(repos) == {"s1": SubTaskStatus.PENDING}
    assert stored(repos, "plan_accepted") == [{"sub_tasks": [{"id": "s1"}]}]


def test_a_plan_whose_event_fails_is_not_stored(repos, monkeypatch):
    fail_events(repos, monkeypatch, "plan_accepted")
    with pytest.raises(Exception, match="injected failure"):
        repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))],
                                 event=event("plan_accepted", sub_tasks=[{"id": "s1"}]))
    assert statuses(repos) == {} and stored(repos, "plan_accepted") == []


def test_an_outcome_and_its_event_are_stored_together(repos):
    repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))])
    repos.subtasks.set_status("alice", "r1", "s1", SubTaskStatus.SUCCEEDED,
                              event=event("subtask_finished", subtask_id="s1", outcome="succeeded"))
    assert statuses(repos) == {"s1": SubTaskStatus.SUCCEEDED}
    assert stored(repos, "subtask_finished") == [{"subtask_id": "s1", "outcome": "succeeded"}]


def test_an_outcome_whose_event_fails_is_not_stored(repos, monkeypatch):
    repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))])
    fail_events(repos, monkeypatch, "subtask_finished")
    with pytest.raises(Exception, match="injected failure"):
        repos.subtasks.set_status("alice", "r1", "s1", SubTaskStatus.SUCCEEDED,
                                  event=event("subtask_finished", subtask_id="s1",
                                              outcome="succeeded"))
    assert statuses(repos) == {"s1": SubTaskStatus.PENDING}
    assert stored(repos, "subtask_finished") == []


def test_an_outcome_for_an_unknown_subtask_logs_nothing(repos):
    repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))])
    with pytest.raises(NotFoundError):
        repos.subtasks.set_status("alice", "r1", "s9", SubTaskStatus.FAILED,
                                  event=event("subtask_finished", subtask_id="s9", outcome="failed"))
    assert stored(repos, "subtask_finished") == []


def test_without_an_event_the_methods_behave_as_before(repos):
    repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))])
    repos.subtasks.set_status("alice", "r1", "s1", SubTaskStatus.FAILED)
    assert statuses(repos) == {"s1": SubTaskStatus.FAILED}
    assert repos.events.read_after("alice", "r1") == []


def test_events_written_with_a_status_keep_one_increasing_sequence():
    repos = SqliteRepos(":memory:")
    try:
        first = repos.events.append(event("stage", state="PLANNING"))
        repos.subtasks.save_plan("alice", "r1", [SubTaskRecord(run_id="r1", subtask=task("s1"))],
                                 event=event("plan_accepted", sub_tasks=[]))
        last = repos.events.append(event("stage", state="RESEARCHING"))
        seqs = [e.seq for e in repos.events.read_after("alice", "r1")]
        assert seqs == sorted(seqs) and seqs[0] == first.seq and seqs[-1] == last.seq
        assert len(seqs) == 3
    finally:
        repos.close()
