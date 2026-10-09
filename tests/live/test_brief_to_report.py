"""Live end-to-end run: real models, real search, real pages. `pytest -m live tests/live -k brief`.

Prints the Run log and Report so a successful run can be recorded in the ticket."""

import json

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.config import Settings
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
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
        run = run_brief(deps, "alice", Brief(text=BRIEF))
    log = repos.events.read_after("alice", run.id)
    print("RUN_LOG", json.dumps([(e.type, e.payload) for e in log], default=str)[:4000])
    assert run.state == RunState.COMPLETE, [e.payload for e in log if e.type == "run_failed"]
    report = repos.reports.get_for_run("alice", run.id)
    print("REPORT", json.dumps(report.body["draft"], indent=1))
    assert any(s["claims"] for s in report.body["draft"]["sections"])
