"""T20 B4: clarification deadlines reconciled without a restart (T9-1), and a shutdown order in
which the scheduler thread never fails and no research is admitted once stopping has begun
(SHUT-1). The real app: APScheduler, the RunExecutor and SQLite files, scripted models."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.schedulers.base import STATE_PAUSED, STATE_STOPPED

from insightforge_agent.domain.errors import ClarificationConflict
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.executor import BoundedRunExecutor, ExecutorClosed
from insightforge_agent.pipeline.research import NOT_STARTED
from insightforge_agent.scheduler import clarification as timeouts_module
from insightforge_agent.scheduler.clarification import (
    RECONCILE_JOB_ID,
    ApsTimeoutScheduler,
    job_id,
)
from tests.api.support import T0, App, wait_until
from tests.api.test_t10_clarification_app import ANSWER, awaiting, types
from tests.api.test_t11_lifecycle import setup, tick
from tests.api.test_t12_diff_app import MutableSearch, complete_tick, diff_done, make_app
from tests.pipeline.test_t9_clarification import Clock

TIMEOUT = timedelta(minutes=30)


def fire_reconciliation(app: App) -> None:
    """Make the persisted reconciliation job due now on the running scheduler."""
    app.runtime.scheduler.modify_job(RECONCILE_JOB_ID, next_run_time=datetime.now(UTC))


def unscheduled(monkeypatch) -> list[str]:
    """Every clarification timeout fails to reach the job store."""
    failed: list[str] = []

    def schedule(self, owner_id, run_id, at):
        failed.append(run_id)
        raise RuntimeError("job store unavailable")

    monkeypatch.setattr(ApsTimeoutScheduler, "schedule", schedule)
    return failed


# --- T9-1: a question whose timeout job is missing still times out, without a restart -------

def test_an_overdue_question_with_no_timeout_job_is_timed_out_by_reconciliation(
        tmp_path, monkeypatch):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        failed = unscheduled(monkeypatch)
        run_id = awaiting(app)
        assert failed == [run_id] and "clarification_timeout_unscheduled" in types(app, run_id)
        assert app.runtime.scheduler.get_job(job_id("alice", run_id)) is None

        assert app.runtime.clarifications.reconcile_overdue() == 0  # before the deadline
        assert app.run(run_id).state == RunState.AWAITING_CLARIFICATION

        clock.advance(TIMEOUT + timedelta(seconds=1))
        fire_reconciliation(app)
        final = app.wait(run_id)
        assert final.state == RunState.COMPLETE
        assert final.clarification.resolution == "timed_out"
        assert types(app, run_id).count("clarification_resolved") == 1
        assert app.client.post(f"/research/{run_id}/clarify",
                               json={"answer": ANSWER}).status_code == 409


def test_reconciliation_and_the_timeout_job_together_resolve_a_question_once(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        run_id = awaiting(app)
        clock.advance(TIMEOUT + timedelta(seconds=1))
        service = app.runtime.clarifications
        barrier = threading.Barrier(4)

        def both(fn):
            barrier.wait()
            fn()

        callers = [threading.Thread(target=both, args=(f,)) for f in (
            service.reconcile_overdue, service.reconcile_overdue,
            lambda: service.time_out("alice", run_id),
            lambda: service.time_out("alice", run_id))]
        for t in callers:
            t.start()
        for t in callers:
            t.join(30)
        assert app.wait(run_id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert types(app, run_id).count("clarification_resolved") == 1


def test_an_answer_overtaken_by_reconciliation_at_the_deadline_loses_and_is_not_stored(
        tmp_path):
    """The answer passes its deadline check just before the deadline, then reconciliation
    resolves the Run first: the compare-and-set keeps the timeout and rejects the answer."""
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        run_id = awaiting(app)
        runs = app.runtime.repos.runs
        real_resolve = runs.resolve_clarification
        in_resolve, release = threading.Event(), threading.Event()

        def resolve(owner_id, rid, **kw):
            if threading.current_thread().name == "answerer":
                in_resolve.set()
                assert release.wait(30)
            return real_resolve(owner_id, rid, **kw)

        runs.resolve_clarification = resolve
        clock.advance(TIMEOUT - timedelta(seconds=1))  # the answer is still in time
        outcome = []

        def answer():
            try:
                app.runtime.clarifications.answer("alice", run_id, ANSWER)
                outcome.append("answered")
            except ClarificationConflict:
                outcome.append("conflict")

        answerer = threading.Thread(target=answer, name="answerer")
        answerer.start()
        assert in_resolve.wait(30)
        clock.advance(timedelta(seconds=2))  # now past the deadline
        assert app.runtime.clarifications.reconcile_overdue() == 1
        release.set()
        answerer.join(30)
        assert outcome == ["conflict"]
        final = app.wait(run_id)
        assert final.clarification.resolution == "timed_out" and final.clarification.answer is None
        assert types(app, run_id).count("clarification_resolved") == 1


def test_reconciliation_keeps_an_answer_and_ignores_unexpired_and_other_runs(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        answered = awaiting(app)
        assert app.client.post(f"/research/{answered}/clarify",
                               json={"answer": ANSWER}).status_code == 202
        assert app.wait(answered).state == RunState.COMPLETE
        clock.advance(timedelta(minutes=10))
        bobs = awaiting(app, "bob")  # deadline T0 + 40 min
        # T0 + 40 min less a second: past `answered`'s deadline, just before bob's.
        clock.advance(TIMEOUT - timedelta(seconds=1))
        assert app.runtime.clarifications.reconcile_overdue() == 0
        assert app.run(answered).clarification.answer == ANSWER
        assert app.run(bobs, "bob").state == RunState.AWAITING_CLARIFICATION
        clock.advance(timedelta(seconds=1))
        assert app.runtime.clarifications.reconcile_overdue() == 1
        assert app.wait(bobs, owner="bob").clarification.resolution == "timed_out"


def test_the_reconciliation_job_is_stored_once_and_survives_a_restart(tmp_path):
    with App(tmp_path) as first:
        job = first.runtime.scheduler.get_job(RECONCILE_JOB_ID)
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time is None
        due = job.next_run_time
        assert timeouts_module._reconcile_handler == first.runtime.clarifications.reconcile_overdue
    assert timeouts_module._reconcile_handler is None
    with App(tmp_path) as second:
        assert second.runtime.scheduler.get_job(RECONCILE_JOB_ID).next_run_time == due


def test_startup_still_times_out_an_overdue_question_whose_job_was_lost(tmp_path, monkeypatch):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as first:
        unscheduled(monkeypatch)
        run_id = awaiting(first)
    monkeypatch.undo()
    clock.advance(TIMEOUT + timedelta(seconds=1))
    with App(tmp_path, now=clock) as second:
        assert second.runtime.recovered["timed_out"] == 1
        assert second.wait(run_id).clarification.resolution == "timed_out"


# --- SHUT-1: shutdown order and admission ---------------------------------------------------

def test_shutdown_while_the_scheduler_thread_holds_a_due_job_raises_nothing_in_it(
        tmp_path, monkeypatch):
    """The scheduler thread is held inside its job-processing pass, holding a due one-shot job,
    until `stop` has gone as far as it can. Before B4, `shutdown` marked the scheduler stopped
    meanwhile, and removing the fired job then raised JobLookupError in that thread."""
    errors: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    app = App(tmp_path)
    with app:
        scheduler = app.runtime.scheduler
        store = scheduler._jobstores["default"]  # test-only: hold the scheduler thread in it
        real_due = store.get_due_jobs
        holding, release = threading.Event(), threading.Event()

        def get_due_jobs(now):
            due = real_due(now)
            if any(j.id == job_id("alice", "run_none") for j in due) and not holding.is_set():
                holding.set()
                release.wait(30)
            return due

        store.get_due_jobs = get_due_jobs
        barrier = threading.Event()  # `stop` asked for the job stores' lock
        real_remove = scheduler.remove_jobstore

        def remove_jobstore(*a, **kw):
            barrier.set()
            return real_remove(*a, **kw)

        scheduler.remove_jobstore = remove_jobstore
        # A due one-shot job (a timeout for a Run that does not exist: its handler is a no-op).
        ApsTimeoutScheduler(scheduler).schedule("alice", "run_none", datetime.now(UTC))
        assert holding.wait(30)
        stopper = threading.Thread(target=app.__exit__, args=(None, None, None))
        stopper.start()
        # Hold until `stop` has either marked the scheduler stopped (the pre-B4 order) or is
        # waiting for the thread to leave its pass; then let the thread finish it.
        wait_until(lambda: barrier.is_set() or scheduler.state == STATE_STOPPED)
        release.set()
        stopper.join(60)
        assert not stopper.is_alive()
    assert errors == []


def test_no_research_is_admitted_once_shutdown_has_begun(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=2)
        app.runtime.begin_shutdown()
        response = app.submit()
        assert response.status_code == 503 and "shutting down" in response.json()["detail"]
        assert tick(app, clock, item) is None
        assert app.runtime.repos.runs.list_for_owner("alice") == []
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)
        assert app.runtime.executor.in_flight == 0


def test_manual_and_watchlist_admission_racing_the_stop_signal_strand_no_run(tmp_path):
    """Requests and a Watchlist tick race `begin_shutdown`. Whatever was admitted first runs
    or is settled; nothing is created afterwards, and no Run is left in its first state."""
    clock = Clock(T0)
    with App(tmp_path, now=clock, run_concurrency=1, run_queue_limit=4) as app:
        item = setup(app, cadence_minutes=15, max_runs=5)
        barrier = threading.Barrier(5)
        results = []

        def manual():
            barrier.wait()
            results.append(app.submit().status_code)

        def watchlist():
            barrier.wait()
            results.append(tick(app, clock, item))

        def stop_signal():
            barrier.wait()
            app.runtime.begin_shutdown()

        threads = [threading.Thread(target=f) for f in (manual, manual, manual, watchlist,
                                                        stop_signal)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert app.submit().status_code == 503  # after the signal, always refused
        assert tick(app, clock, item) is None
        created = app.runtime.repos.runs.list_for_owner("alice")
        statuses = [r for r in results if isinstance(r, int)]
        assert len(created) == statuses.count(202) + sum(
            1 for r in results if r is not None and not isinstance(r, int))
        assert set(statuses) <= {202, 503}
    with App(tmp_path, now=clock) as again:  # whatever was admitted ended, or was settled
        for run in created:
            final = again.wait(run.id)
            assert final.state in (RunState.COMPLETE, RunState.FAILED)
            if final.state == RunState.FAILED:
                assert any(e.payload.get("reason") == NOT_STARTED
                           for e in again.events(run.id))


def test_a_run_created_as_admission_closes_is_failed_not_stranded():
    executor = BoundedRunExecutor(1, 1)
    cancelled, ran = [], []

    def create():
        executor.close()  # shutdown begins between creating the Run and queueing it
        return "run_1"

    with pytest.raises(ExecutorClosed):
        executor.admit(create, ran.append, cancelled.append)
    assert cancelled == ["run_1"] and ran == []
    with pytest.raises(ExecutorClosed):
        executor.submit(lambda: None)
    executor.shutdown()


def test_a_watchlist_run_finishing_during_shutdown_is_diffed_once_after_restart(tmp_path):
    clock, search = Clock(T0), MutableSearch()
    app, _ = make_app(tmp_path, clock, search)
    with app:
        item = setup(app, cadence_minutes=15, max_runs=3)
        complete_tick(app, clock, item)
        wait_until(lambda: app.runtime.repos.watchlists.baseline_state("alice", item.id)
                   == "established")
        search.price = "29,000"
        app.gate.close()
        app.gate.fetching.clear()
        second = tick(app, clock, item)
        assert app.gate.fetching.wait(30)  # held mid-research
        stopper = threading.Thread(target=app.runtime.stop)
        stopper.start()
        wait_until(lambda: app.runtime.scheduler.state == STATE_PAUSED
                   and app.runtime.closing.is_set())
        # Admission is closed while the Run is still running on open resources.
        assert app.runtime.repos.runs.get("alice", second.id).state == RunState.RESEARCHING
        with pytest.raises(ExecutorClosed):
            app.runtime.executor.submit(lambda: None)
        app.gate.open()
        stopper.join(60)
        assert not stopper.is_alive()
    app2, _ = make_app(tmp_path, clock, search)
    with app2:
        assert app2.run(second.id).state == RunState.COMPLETE  # the active Run finished
        wait_until(lambda: diff_done(app2, item, second.id))
        diffs = app2.runtime.repos.diffs
        assert [d.status for d in diffs.diffs("alice", item.id)] == ["succeeded"]
        assert len(diffs.alerts("alice")) == 1
    with make_app(tmp_path, clock, search)[0] as app3:  # a second restart repeats nothing
        wait_until(lambda: app3.runtime.scheduler.get_job(
            f"watchlist-diff:alice:{item.id}") is None)
        assert len(app3.runtime.repos.diffs.alerts("alice")) == 1
        assert len(app3.runtime.repos.diffs.diffs("alice", item.id)) == 1
