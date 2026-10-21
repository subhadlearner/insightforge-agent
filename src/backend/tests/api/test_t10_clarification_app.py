"""T10 x T9: clarification through the running application. The router is mounted with the
persona dependency, ClarificationService submits to the RunExecutor, the persistent APScheduler
scheduler fires timeouts into the installed handler, and the startup sweep recovers Runs across
real restarts (a new Runtime over the same SQLite files)."""

import sqlite3
import threading
from datetime import timedelta

import pytest
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING

from insightforge_agent.api import app as app_module
from insightforge_agent.domain.models import Run, RunState
from insightforge_agent.pipeline.executor import ExecutorClosed
from insightforge_agent.scheduler import clarification as timeouts_module
from insightforge_agent.scheduler.retention import JOB_ID as RETENTION_JOB
from insightforge_agent.scheduler.clarification import (
    JOB_TABLE,
    RECONCILE_JOB_ID,
    ApsTimeoutScheduler,
    job_id,
)
from tests.api.support import QUESTION, T0, VAGUE, App, Gate, wait_until
from tests.pipeline import fakes
from tests.pipeline.test_t9_clarification import Clock

TIMEOUT = timedelta(minutes=30)
ANSWER = "Electric vehicles in China and the US, 2025: BYD and Tesla sales and pricing."


def stored_jobs(tmp_path) -> list[str]:
    """The stored jobs other than the app's daily retention job (T13) and its clarification
    reconciliation job (T20 B4), which are always there once the app has started."""
    db = sqlite3.connect(tmp_path / "app.db")
    try:
        ids = [r[0] for r in db.execute(f"SELECT id FROM {JOB_TABLE}")]
    finally:
        db.close()
    assert RETENTION_JOB in ids and RECONCILE_JOB_ID in ids
    return [i for i in ids if i not in (RETENTION_JOB, RECONCILE_JOB_ID)]


def awaiting(app: App, persona: str = "alice") -> str:
    app.login(persona)
    response = app.submit(VAGUE)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    app.wait(run_id, lambda r: r.state == RunState.AWAITING_CLARIFICATION, owner=persona)
    # The question is stored first, then its timeout job: wait for the Run's work to finish.
    wait_until(lambda: "clarification_requested" in types(app, run_id, persona)
               and app.runtime.executor.in_flight == 0)
    return run_id


def types(app: App, run_id: str, owner: str = "alice") -> list[str]:
    return [e.type for e in app.events(run_id, owner)]


# --- endpoints, answer, resume -------------------------------------------------------------

def test_the_question_is_served_and_the_answer_resumes_the_run_through_the_executor(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        run_id = awaiting(app)
        assert stored_jobs(tmp_path) == [job_id("alice", run_id)]  # the real job store
        got = app.client.get(f"/research/{run_id}/clarification")
        assert got.status_code == 200
        assert got.json()["question"] == QUESTION and got.json()["resolution"] is None
        response = app.client.post(f"/research/{run_id}/clarify", json={"answer": ANSWER})
        assert response.status_code == 202 and response.json()["state"] == "RESUMING"
        final = app.wait(run_id)
        assert final.state == RunState.COMPLETE
        assert final.clarification.resolution == "answered"
        assert app.runtime.repos.reports.get_for_run("alice", run_id)
        assert stored_jobs(tmp_path) == []  # the answer cancelled the timeout job
        seen = types(app, run_id)
        assert seen.index("clarification_resolved") < seen.index("resuming")
        assert app.client.get(f"/research/{run_id}/clarification").json()["resolution"] == \
            "answered"


def test_a_duplicate_or_late_answer_is_409_and_bad_payloads_are_422(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        answered = awaiting(app)
        assert app.client.post(f"/research/{answered}/clarify",
                               json={"answer": ANSWER}).status_code == 202
        assert app.client.post(f"/research/{answered}/clarify",
                               json={"answer": ANSWER}).status_code == 409
        late = awaiting(app)
        for body in ({"answer": ""}, {"answer": "x" * 2001}, {}, {"answer": "   "}):
            assert app.client.post(f"/research/{late}/clarify", json=body).status_code == 422
        clock.advance(TIMEOUT)
        response = app.client.post(f"/research/{late}/clarify", json={"answer": ANSWER})
        assert response.status_code == 409
        assert app.run(late).state == RunState.AWAITING_CLARIFICATION  # left for the timeout
        app.wait(answered)


def test_clarification_endpoints_are_owner_scoped(tmp_path):
    with App(tmp_path, now=Clock(T0)) as app:
        run_id = awaiting(app, "bob")
        app.login("alice")
        for response in (app.client.get(f"/research/{run_id}/clarification"),
                         app.client.post(f"/research/{run_id}/clarify",
                                         json={"answer": ANSWER})):
            assert response.status_code == 404
        assert app.client.get("/research/run_nope/clarification").status_code == 404
        assert app.run(run_id, "bob").state == RunState.AWAITING_CLARIFICATION
        app.client.cookies.clear()
        assert app.client.get(f"/research/{run_id}/clarification").status_code == 401


# --- the timeout, fired by the running scheduler ---------------------------------------------

def test_the_running_scheduler_fires_the_timeout_and_the_run_resumes_on_the_executor(
        tmp_path):
    with App(tmp_path, deps_overrides={"clarification_timeout": timedelta(seconds=1)}) as app:
        run_id = awaiting(app)
        final = app.wait(run_id, timeout=60)
        assert final.state == RunState.COMPLETE
        assert final.clarification.resolution == "timed_out"
        report = app.runtime.repos.reports.get_for_run("alice", run_id)
        assert "was not answered before its deadline" in report.body["draft"]["gaps"][0]
        assert stored_jobs(tmp_path) == []
        assert app.client.post(f"/research/{run_id}/clarify",
                               json={"answer": ANSWER}).status_code == 409


# --- startup ordering, recovery across restarts ----------------------------------------------

def test_startup_sweeps_with_the_scheduler_paused_and_the_handler_installed(
        tmp_path, monkeypatch):
    seen = {}
    real_sweep = app_module.startup_sweep

    def sweep(deps, clarifications):
        runtime = observed["app"].runtime
        seen["scheduler"] = runtime.scheduler.state
        seen["handler"] = timeouts_module._handler
        return real_sweep(deps, clarifications)

    monkeypatch.setattr(app_module, "startup_sweep", sweep)
    observed = {"app": App(tmp_path)}
    with observed["app"] as app:
        assert seen["scheduler"] == STATE_PAUSED  # jobs writable, none firing yet
        assert seen["handler"] == app.runtime.clarifications.time_out
        assert app.runtime.scheduler.state == STATE_RUNNING  # resumed after the sweep


def test_a_restart_recovers_every_unfinished_run(tmp_path):
    """RESUMING is resumed, an expired question is timed out, an unexpired one waits with its
    job scheduled again, and an in-progress Run is FAILED, all before requests are served."""
    clock = Clock(T0)
    with App(tmp_path, now=clock) as first:
        expired = awaiting(first)  # deadline T0 + 30 min
        resuming = awaiting(first)
        # A crash right after the answer's compare-and-set: RESUMING, and nothing resumed it.
        first.runtime.repos.runs.resolve_clarification("alice", resuming, answer=ANSWER,
                                                       resolved_at=clock())
        clock.advance(timedelta(minutes=20))
        waiting = awaiting(first)  # deadline T0 + 50 min
        ApsTimeoutScheduler(first.runtime.scheduler).cancel("alice", waiting)  # its job lost
        deadline = first.run(waiting).clarification.deadline
        first.runtime.repos.runs.add(Run(id="run_crashed", owner_id="alice", brief="b",
                                         state=RunState.RESEARCHING, created_at=T0))
    # The crash left `resuming` with its job; `waiting` lost its own.
    assert sorted(stored_jobs(tmp_path)) == sorted([job_id("alice", expired),
                                                    job_id("alice", resuming)])

    clock.advance(timedelta(minutes=15))  # T0 + 35 min: `expired` is past its deadline
    with App(tmp_path, now=clock) as second:
        assert second.runtime.recovered == {"timed_out": 1, "resumed": 1,
                                            "still_awaiting": 1, "failed": 1}
        assert second.wait(resuming).state == RunState.COMPLETE
        assert second.run(resuming).clarification.resolution == "answered"
        timed_out = second.wait(expired)
        assert timed_out.state == RunState.COMPLETE
        assert timed_out.clarification.resolution == "timed_out"
        assert second.run(waiting).state == RunState.AWAITING_CLARIFICATION
        assert ApsTimeoutScheduler(second.runtime.scheduler).scheduled_at(
            "alice", waiting) == deadline
        assert second.run("run_crashed").state == RunState.FAILED
        second.login("alice")
        assert second.client.post(f"/research/{waiting}/clarify",
                                  json={"answer": ANSWER}).status_code == 202
        assert second.wait(waiting).state == RunState.COMPLETE


# --- a resume the executor cannot take is deferred to the next startup, never lost -------------

def test_an_answer_the_executor_refuses_leaves_the_run_resuming_for_the_next_startup(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as first:
        run_id = awaiting(first)
        first.runtime.executor.shutdown()  # the process is stopping
        response = first.client.post(f"/research/{run_id}/clarify", json={"answer": ANSWER})
        assert response.status_code == 202  # the answer is stored: the compare-and-set won
        assert first.run(run_id).state == RunState.RESUMING
        assert first.run(run_id).clarification.answer == ANSWER
        deferred = [e.payload for e in first.events(run_id) if e.type == "resume_deferred"]
        assert len(deferred) == 1 and "did not accept" in deferred[0]["reason"]
        assert first.client.post(f"/research/{run_id}/clarify",
                                 json={"answer": ANSWER}).status_code == 409
    with App(tmp_path, now=clock) as second:
        assert second.runtime.recovered["resumed"] == 1
        final = second.wait(run_id)
        assert final.state == RunState.COMPLETE and final.clarification.answer == ANSWER


def test_a_queued_resume_cancelled_at_shutdown_is_deferred_not_lost(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, Gate(fakes.FixtureSearch(), open_=False), now=clock,
             run_concurrency=1, run_queue_limit=1) as first:
        run_id = awaiting(first)
        busy = first.submit().json()["run_id"]  # holds the only worker at its first fetch
        assert first.gate.fetching.wait(30)
        assert first.client.post(f"/research/{run_id}/clarify",
                                 json={"answer": ANSWER}).status_code == 202
        stopper = threading.Thread(target=first.runtime.executor.shutdown)
        stopper.start()
        wait_until(lambda: "resume_deferred" in types(first, run_id))
        first.gate.open()
        stopper.join(60)
        assert first.run(busy).state == RunState.COMPLETE  # the running Run finished
        assert first.run(run_id).state == RunState.RESUMING
    with App(tmp_path, now=clock) as second:
        assert second.wait(run_id).state == RunState.COMPLETE


# --- shutdown -----------------------------------------------------------------------------

def test_shutdown_stops_the_scheduler_and_executor_and_closes_the_database(tmp_path):
    with App(tmp_path) as app:
        runtime = app.runtime
        assert runtime.scheduler.running and timeouts_module._handler is not None
    assert not runtime.scheduler.running
    assert timeouts_module._handler is None
    with pytest.raises(ExecutorClosed):
        runtime.executor.submit(lambda: None)
    with pytest.raises(sqlite3.ProgrammingError):
        runtime.repos.runs.list_for_owner("alice")
