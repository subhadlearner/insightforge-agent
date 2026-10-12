"""T9: clarification timeouts as persisted one-shot APScheduler jobs (real APScheduler, real
SQLAlchemy job store in the application's SQLite file)."""

import sqlite3
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import pytest

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.scheduler import clarification as timeouts_module
from insightforge_agent.scheduler.clarification import (
    JOB_TABLE,
    ApsTimeoutScheduler,
    build_scheduler,
    fire_timeout,
    install_timeout_handler,
    job_id,
)
from tests.pipeline.test_t9_clarification import (
    TIMEOUT,
    VAGUE,
    Clock,
    Process,
    ask_turn,
    plan_turn,
    research_turn,
    scripted,
)

FUTURE = datetime(2099, 1, 1, 12, tzinfo=UTC)  # never due while a test runs


@pytest.fixture
def started(tmp_path):
    """Start schedulers over the app database; each is shut down after the test. A paused
    scheduler still writes jobs to its store."""
    url = f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    made = []

    def start(paused: bool = True):
        scheduler = build_scheduler(url)
        scheduler.start(paused=paused)
        made.append(scheduler)
        return scheduler

    yield start
    for s in made:
        if s.running:
            s.shutdown(wait=True)
    install_timeout_handler(None)


def stored_jobs(tmp_path) -> list[str]:
    db = sqlite3.connect(tmp_path / "app.db")
    try:
        return [r[0] for r in db.execute(f"SELECT id FROM {JOB_TABLE}")]
    finally:
        db.close()


def test_a_timeout_is_a_job_in_the_database_at_the_deadline_and_survives_a_restart(
        started, tmp_path):
    scheduler = started()
    ApsTimeoutScheduler(scheduler).schedule("alice", "run_1", FUTURE)
    assert stored_jobs(tmp_path) == [job_id("alice", "run_1")]
    scheduler.shutdown(wait=True)  # the process stops; nothing in memory survives

    restarted = ApsTimeoutScheduler(started())
    assert restarted.scheduled_at("alice", "run_1") == FUTURE
    job = restarted._scheduler.get_job(job_id("alice", "run_1"))
    assert job.func_ref == "insightforge_agent.scheduler.clarification:fire_timeout"
    assert job.args == ("alice", "run_1")


# APScheduler 3.11's loop thread wakes on shutdown, tries to submit a due job to its closed
# executor and then fails to delete it (a stopped scheduler only looks in its pending list):
# a logged thread exception, but the stored job survives. These tests pin that down.
SHUTDOWN_NOISE = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")


@SHUTDOWN_NOISE
def test_shutting_down_with_a_due_job_keeps_it_for_the_next_start(started, tmp_path):
    """A timeout that fell due while the process was stopping is still stored afterwards."""
    for _ in range(5):
        scheduler = started()
        ApsTimeoutScheduler(scheduler).schedule("alice", "run_1", datetime(2020, 1, 1, tzinfo=UTC))
        scheduler.shutdown(wait=True)
        assert stored_jobs(tmp_path) == [job_id("alice", "run_1")]


def test_rescheduling_keeps_one_job_per_run_and_cancel_removes_it(started, tmp_path):
    timeouts = ApsTimeoutScheduler(started())
    timeouts.schedule("alice", "run_1", FUTURE)
    timeouts.schedule("alice", "run_1", FUTURE + timedelta(minutes=1))  # the sweep again
    timeouts.schedule("bob", "run_1", FUTURE)
    assert sorted(stored_jobs(tmp_path)) == sorted([job_id("alice", "run_1"),
                                                    job_id("bob", "run_1")])
    assert timeouts.scheduled_at("alice", "run_1") == FUTURE + timedelta(minutes=1)
    timeouts.cancel("alice", "run_1")
    timeouts.cancel("alice", "run_1")  # already gone: not an error
    assert stored_jobs(tmp_path) == [job_id("bob", "run_1")]


@SHUTDOWN_NOISE
def test_a_fired_job_times_the_run_out_through_the_shared_compare_and_set(started, tmp_path):
    """End to end on the real scheduler: the Run waits with its job stored; after a restart
    the job (due while the process was down) fires, and the installed handler resolves the
    Run as a timeout and carries it on."""
    asked = datetime(2026, 1, 1, 9, tzinfo=UTC)  # in the past, so the job is due at once
    with ExitStack() as stack:
        first = started()
        proc = Process(tmp_path, stack, scripted(ask_turn()), Clock(asked),
                       timeouts=ApsTimeoutScheduler(first))
        run =run_brief(proc.deps, "alice", Brief(text=VAGUE))
        assert run.state == RunState.AWAITING_CLARIFICATION
        assert stored_jobs(tmp_path) == [job_id("alice", run.id)]
        first.shutdown(wait=True)

        proc.clock.advance(TIMEOUT)  # the fake clock agrees the deadline has passed
        second = started()
        after = proc.restart(scripted(plan_turn(assumption="global EV makers"),
                                      research_turn()),
                             timeouts=ApsTimeoutScheduler(second))
        done = threading.Event()

        def handler(owner_id, run_id):
            try:
                after.service.time_out(owner_id, run_id)
            finally:
                done.set()

        install_timeout_handler(handler)
        assert ApsTimeoutScheduler(second).scheduled_at("alice", run.id) == asked + TIMEOUT
        second.resume()
        assert done.wait(60)
        final = after.run(run.id)
        assert final.state == RunState.COMPLETE, [e.payload for e in after.events(run.id)]
        assert final.clarification.resolution == "timed_out"
        assert stored_jobs(tmp_path) == []  # a one-shot job is gone once it ran


def test_a_job_firing_with_no_handler_installed_changes_nothing(caplog):
    install_timeout_handler(None)
    fire_timeout("alice", "run_1")
    assert "no handler" in caplog.text
    assert timeouts_module._handler is None
