"""Live end-to-end run: real models, real search, real pages. `pytest -m live tests/live -k brief`.

Prints the Run log and Report so a successful run can be recorded in the ticket."""

import json

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.config import Settings
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
from insightforge_agent.domain.plan import SubTaskPlan, plan_violations
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.pipeline.wiring import build_deps
from insightforge_agent.stores.memory import MemoryRepos

pytestmark = pytest.mark.live

BRIEF = ("Compare BYD and Tesla in battery electric vehicles: 2025 sales volume, "
         "pricing strategy and battery technology.")


def test_brief_to_report_live():
    repos = MemoryRepos()
    with SqliteSaver.from_conn_string(":memory:") as saver:
        deps = build_deps(Settings(), repos, saver)
        snapshots: list[dict] = []
        run = run_brief(deps, "alice", Brief(text=BRIEF), on_state=snapshots.append)
    log = repos.events.read_after("alice", run.id, limit=1_000_000)
    print("EVENT_TYPES", json.dumps([e.type for e in log]))
    for e in log:
        if e.type == "writer_violations":
            print("WRITER_VIOLATIONS", json.dumps(e.payload)[:1500])
    for e in log:
        if e.type in {"dispatch_rejected", "plan_correction", "plan_trimmed", "repair",
                      "subtask_failed"}:
            print("T4_EVENT", e.type, json.dumps(e.payload))
    assert run.state == RunState.COMPLETE, [e.payload for e in log if e.type == "run_failed"]
    plan = SubTaskPlan.model_validate(next(s["plan"] for s in reversed(snapshots) if "plan" in s))
    print("T4_PLAN", json.dumps(plan.model_dump()))
    assert 3 <= len(plan.sub_tasks) <= 6
    assert {t.aspect_id for t in plan.sub_tasks} == {a.id for a in plan.aspects}
    assert plan_violations(plan, ["web"]) == []
    reports = repos.reports.list_for_owner("alice")
    assert len(reports) == 1
    report = reports[0]
    s = Settings()
    print("RECORD", json.dumps({
        "provider": s.llm_provider,
        "models": {r: getattr(s, f"{s.llm_provider}_{r}_model")
                   for r in ("planner", "writer", "light")},
        "run_id": run.id, "report_id": report.id, "final_state": run.state.value,
        "stages": [e.payload["state"] for e in log if e.type == "stage"],
        "reports_stored": len(reports),
    }))
    print("REPORT", json.dumps(report.body["draft"], indent=1))
    assert any(s["claims"] for s in report.body["draft"]["sections"])
