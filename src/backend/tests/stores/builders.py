from datetime import UTC, datetime

from insightforge_agent.domain.models import (
    Observation,
    Passage,
    Report,
    Run,
    RunEvent,
    RunState,
    Source,
    SubTaskRecord,
)
from insightforge_agent.domain.plan import SubTask

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def run(id="r1", owner="alice", state=RunState.PLANNING, **kw) -> Run:
    return Run(id=id, owner_id=owner, brief="Compare EV makers", state=state, created_at=T0, **kw)


def subtask(id="s1", run_id="r1", **kw) -> SubTaskRecord:
    st = SubTask(id=id, aspect_id="a1", query=f"query {id}", source_type="web",
                 time_horizon_months=12, priority=1)
    return SubTaskRecord(run_id=run_id, subtask=st, **kw)


def source(id="src_1", owner="alice", run_id="r1", **kw) -> Source:
    return Source(id=id, owner_id=owner, run_id=run_id, kind="web",
                  locator="https://example.com/a", title="Example", **kw)


def observation(id="obs_1", owner="alice", source_id="src_1", run_id="r1", at=T0) -> Observation:
    return Observation(id=id, owner_id=owner, source_id=source_id, run_id=run_id, fetched_at=at)


def passage(index, text=None, obs="obs_1", **kw) -> Passage:
    return Passage(observation_id=obs, index=index, text=text or f"paragraph {index}", **kw)


def report(id="rep1", owner="alice", run_id="r1") -> Report:
    return Report(id=id, owner_id=owner, run_id=run_id, created_at=T0, body={"sections": [1, 2]})


def event(owner="alice", run_id="r1", type="stage", **payload) -> RunEvent:
    return RunEvent(seq=0, owner_id=owner, run_id=run_id, type=type, payload=payload, created_at=T0)
