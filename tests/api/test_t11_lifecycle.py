from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
import threading

from insightforge_agent.domain.models import RunState
from insightforge_agent.domain.watchlist import BASELINE_SEED_SCHEMA_VERSION
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.scheduler.watchlist import (
    BASELINE_RETRY_DELAY,
    baseline_job_id,
    job_id,
)
from tests.api.support import App, BRIEF, Gate, T0, wait_until
from tests.api.test_t11_watchlist import create
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, ai
from tests.pipeline.test_t9_clarification import Clock


def setup(app, **kw):
    app.login("alice")
    response = create(app, **kw)
    assert response.status_code == 201, response.text
    return app.runtime.repos.watchlists.get("alice", response.json()["item"]["id"])


def tick(app, clock, item):
    current = app.runtime.repos.watchlists.get("alice", item.id)
    clock.at = current.next_tick_at
    return app.runtime.watchlists.schedules.tick("alice", item.id, current.revision)


def set_due(app, item, due):
    """Arrange a real APScheduler timing test without invoking PATCH merge semantics."""
    item = item.model_copy(update={"next_tick_at": due})
    with app.runtime.repos.lock, app.runtime.repos._db:
        app.runtime.repos._db.execute(
            "UPDATE watchlist_items SET item=? WHERE owner_id=? AND id=?",
            (item.model_dump_json(), item.owner_id, item.id),
        )
    return item


def test_ticks_overlap_limit_counts_baseline_and_reports_survive_delete(tmp_path):
    clock = Clock(T0)
    gate = Gate(fakes.FixtureSearch(), open_=False)
    with App(tmp_path, gate=gate, now=clock) as app:
        item = setup(app, cadence_minutes=1, max_runs=2)
        schedules = app.runtime.watchlists.schedules
        job = app.runtime.scheduler.get_job(job_id("alice", item.id))
        assert job.max_instances == 1 and job.coalesce and job.misfire_grace_time is None
        clock.at = item.next_tick_at
        barrier = threading.Barrier(6)

        def call():
            barrier.wait()
            return schedules.tick("alice", item.id, item.revision)

        with ThreadPoolExecutor(6) as pool:
            attempts = list(pool.map(lambda _: call(), range(6)))
        runs = [r for r in attempts if r]
        assert len(runs) == 1
        assert gate.fetching.wait(30)
        assert tick(app, clock, item) is None  # still researching
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (1, 0, 0)
        gate.open()
        assert app.wait(runs[0].id).state == RunState.COMPLETE
        wait_until(
            lambda: app.runtime.repos.watchlists.baseline("alice", item.id) is not None
        )
        baseline = app.runtime.repos.watchlists.baseline("alice", item.id)
        assert baseline.seeds
        assert baseline.normalization_pending
        assert all(
            s.run_id == runs[0].id
            and s.evidence.primary.observation_id in s.observation_dates
            for s in baseline.seeds
        )
        assert not any(s.claim.historical for s in baseline.seeds)
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        second = tick(app, clock, item)
        assert app.wait(second.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) == baseline
        assert tick(app, clock, item) is None
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None
        data = app.client.get(f"/watchlist/{item.id}").json()
        assert (data["runs_started"], data["completed_runs"], data["failed_runs"]) == (
            2,
            2,
            0,
        )
        for r in (runs[0], second):
            assert not any(
                "diff" in e.type or "alert" in e.type for e in app.events(r.id)
            )
        assert (
            app.client.patch(
                f"/watchlist/{item.id}",
                json={"revision": item.revision, "brief": "Different research"},
            ).status_code
            == 409
        )
        assert app.client.delete(f"/watchlist/{item.id}").status_code == 204
        assert len(app.client.get("/reports").json()) == 2


def test_capacity_refusal_consumes_no_slot(tmp_path):
    clock = Clock(T0)
    gate = Gate(fakes.FixtureSearch(), open_=False)
    with App(
        tmp_path, gate=gate, now=clock, run_concurrency=1, run_queue_limit=0
    ) as app:
        item = setup(app, max_runs=1)
        one = app.submit().json()["run_id"]
        assert gate.fetching.wait(30)
        assert tick(app, clock, item) is None
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)
        # Same refused tick is not retried after capacity becomes available.
        gate.open()
        app.wait(one)
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert (
            app.runtime.watchlists.schedules.tick("alice", item.id, item.revision)
            is None
        )
        accepted = tick(app, clock, item)
        assert accepted is not None
        assert app.wait(accepted.id).state == RunState.COMPLETE


def test_failed_run_consumes_slot_without_baseline_then_success_initializes(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=3)
        deps = app.runtime.deps
        writer = deps.writer_model
        deps.writer_model = ScriptedChat(reply=lambda _: ai(text="invalid JSON"))
        failed = tick(app, clock, item)
        assert app.wait(failed.id).state == RunState.FAILED
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) is None
        deps.writer_model = writer
        successful = tick(app, clock, item)
        assert app.wait(successful.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        baseline = app.runtime.repos.watchlists.baseline("alice", item.id)
        assert baseline.initializing_run_id == successful.id
        deps.writer_model = ScriptedChat(reply=lambda _: ai(text="invalid JSON"))
        last = tick(app, clock, item)
        assert app.wait(last.id).state == RunState.FAILED
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) == baseline
        data = app.client.get(f"/watchlist/{item.id}").json()
        assert (data["runs_started"], data["completed_runs"], data["failed_runs"]) == (
            3,
            1,
            2,
        )
        assert [r["id"] for r in data["recent_failed_runs"]] == [last.id, failed.id]
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None
        assert not any(
            "alert" in e.type or "diff" in e.type for e in app.events(last.id)
        )


def test_restart_skips_missed_ticks_and_recovers_abandoned_run(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=2)
        from insightforge_agent.pipeline.graph import check_brief, start_run

        abandoned = start_run(
            app.runtime.deps,
            check_brief(
                app.runtime.deps, "alice", item.as_brief(), watchlist_item_id=item.id
            ),
        )
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is not None
    clock.at += timedelta(hours=2, seconds=5)
    with App(tmp_path, now=clock) as app:
        current = app.runtime.repos.watchlists.get("alice", item.id)
        assert app.run(abandoned.run.id).state == RunState.FAILED
        assert current.next_tick_at > clock.at
        job = app.runtime.scheduler.get_job(job_id("alice", item.id))
        assert job.next_run_time == current.next_tick_at
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (1, 0, 1)
        run = tick(app, clock, current)
        assert app.wait(run.id).state == RunState.COMPLETE
    with App(tmp_path, now=clock) as app:
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (2, 1, 1)


def test_update_and_delete_reject_pending_tick(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app)
        clock.at = item.next_tick_at
        assert (
            app.client.patch(
                    f"/watchlist/{item.id}",
                    json={"revision": item.revision, "cadence_minutes": 30},
            ).status_code
            == 200
        )
        assert (
            app.runtime.watchlists.schedules.tick("alice", item.id, item.revision)
            is None
        )
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)
        current = app.runtime.repos.watchlists.get("alice", item.id)
        clock.at = current.next_tick_at
        app.client.delete(f"/watchlist/{item.id}")
        assert (
            app.runtime.watchlists.schedules.tick("alice", item.id, current.revision)
            is None
        )
        assert app.runtime.repos.runs.list_for_owner("alice") == []


def test_deletion_during_active_run_preserves_report_and_no_baseline(tmp_path):
    clock = Clock(T0)
    gate = Gate(fakes.FixtureSearch(), open_=False)
    with App(tmp_path, gate=gate, now=clock) as app:
        item = setup(app)
        run = tick(app, clock, item)
        assert gate.fetching.wait(30)
        assert app.client.delete(f"/watchlist/{item.id}").status_code == 204
        gate.open()
        assert app.wait(run.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.client.get("/watchlist").json() == []
        assert len(app.client.get("/reports").json()) == 1


def test_real_apscheduler_fires_through_bounded_executor(tmp_path):
    from datetime import UTC, datetime

    with App(tmp_path) as app:
        item = setup(app, max_runs=1)
        due = datetime.now(UTC) + timedelta(seconds=0.3)
        item = set_due(app, item, due)
        app.runtime.watchlists.schedules.sync(item)
        wait_until(lambda: bool(app.runtime.repos.runs.list_for_owner("alice")))
        run = app.runtime.repos.runs.list_for_owner("alice")[0]
        assert run.watchlist_item_id == item.id
        assert app.wait(run.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None
        assert app.runtime.repos.watchlists.baseline("alice", item.id).seeds


def test_queued_cancellation_consumes_started_slot_and_active_worker_finishes(tmp_path):
    clock = Clock(T0)
    gate = Gate(fakes.FixtureSearch(), open_=False)
    with App(
        tmp_path, gate=gate, now=clock, run_concurrency=1, run_queue_limit=1
    ) as app:
        first = setup(app, max_runs=1)
        second = setup(app, max_runs=1)
        active = tick(app, clock, first)
        assert gate.fetching.wait(30)
        queued = tick(app, clock, second)
        stopped = threading.Event()

        def stop():
            app.runtime.executor.shutdown()
            stopped.set()

        thread = threading.Thread(target=stop)
        thread.start()
        try:
            assert app.wait(queued.id).state == RunState.FAILED
            assert not stopped.is_set()
            assert app.runtime.repos.watchlists.counts("alice", second.id) == (1, 0, 1)
            assert app.runtime.repos.watchlists.baseline("alice", second.id) is None
        finally:
            gate.open()
            thread.join(30)
        assert stopped.is_set()
        assert app.run(active.id).state == RunState.COMPLETE
        assert app.runtime.repos.watchlists.baseline("alice", first.id) is not None


def test_restart_repairs_completion_before_baseline_without_replacing_first_success(
    tmp_path,
):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=2)
        # Simulate process loss after COMPLETE and before its post-processing callback.
        app.runtime.deps.on_complete = None
        first = tick(app, clock, item)
        assert app.wait(first.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) is None
    clock.at += timedelta(hours=1)
    with App(tmp_path, now=clock) as app:
        baseline = app.runtime.repos.watchlists.baseline("alice", item.id)
        assert baseline.initializing_run_id == first.id and baseline.seeds
        second = tick(app, clock, item)
        assert app.wait(second.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) == baseline


def test_clarification_wait_and_resume_block_ticks_without_consuming_more_slots(
    tmp_path,
):
    # Existing T9 checkpoints can survive into T11; do not replay any research to convert one.
    from tests.api.support import VAGUE

    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=2)
        one_shot = app.submit(brief=VAGUE).json()["run_id"]
        app.wait(one_shot, until=lambda r: r.state == RunState.AWAITING_CLARIFICATION)
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        # Migration fixture: attach the durable awaiting Run to the item's history.
        with app.runtime.repos.lock, app.runtime.repos._db:
            app.runtime.repos._db.execute(
                "UPDATE runs SET watchlist_item_id=? WHERE id=?", (item.id, one_shot)
            )
        assert tick(app, clock, item) is None
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (1, 0, 0)
        # Resolve before the T9 immutable deadline.
        response = app.client.post(
            f"/research/{one_shot}/clarify", json={"answer": BRIEF}
        )
        assert response.status_code == 202, response.text
        assert app.wait(one_shot).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) is not None
        second = tick(app, clock, item)
        assert app.wait(second.id).state == RunState.COMPLETE
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (2, 2, 0)


def test_real_apscheduler_accepts_delay_shorter_than_one_cadence(tmp_path):
    from datetime import UTC, datetime

    with App(tmp_path) as app:
        item = setup(app, max_runs=1)
        scheduler = app.runtime.scheduler
        scheduler.pause()
        item = set_due(
            app,
            item,
            datetime.now(UTC) - timedelta(seconds=10),
        )
        app.runtime.watchlists.schedules.sync(item)
        scheduler.resume()
        wait_until(lambda: bool(app.runtime.repos.runs.list_for_owner("alice")))
        started = app.runtime.repos.runs.list_for_owner("alice")[0]
        assert app.wait(started.id).state == RunState.COMPLETE
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (1, 1, 0)


def test_baseline_processing_failure_does_not_block_next_run_or_change_outcomes(
    tmp_path, monkeypatch
):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=2)
        schedules = app.runtime.watchlists.schedules
        initialize = schedules.initialize

        def unavailable(*_):
            raise RuntimeError("injected Baseline persistence outage")

        monkeypatch.setattr(schedules, "initialize", unavailable)
        first = tick(app, clock, item)
        assert app.wait(first.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.baseline("alice", item.id) is None
        second = tick(app, clock, item)
        assert app.wait(second.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (2, 2, 0)
        # Recovery still initializes from the FIRST success, not the most recent Report.
        monkeypatch.setattr(schedules, "initialize", initialize)
        schedules.recover()
        assert (
            app.runtime.repos.watchlists.baseline("alice", item.id).initializing_run_id
            == first.id
        )


def test_begin_shutdown_never_waits_for_repository_lock(tmp_path):
    with App(tmp_path) as app:
        locked = threading.Event()
        release = threading.Event()

        def hold_repository_lock():
            with app.runtime.repos.lock:
                locked.set()
                release.wait(10)

        holder = threading.Thread(target=hold_repository_lock)
        holder.start()
        assert locked.wait(5)
        returned = threading.Event()

        def signal_shutdown():
            app.runtime.begin_shutdown()
            returned.set()

        caller = threading.Thread(target=signal_shutdown)
        caller.start()
        try:
            assert returned.wait(1)
            assert app.runtime.closing.is_set()
            assert app.runtime.watchlists.schedules.closing.is_set()
        finally:
            release.set()
            holder.join(5)
            caller.join(5)


def test_tick_revision_rejects_old_brief_after_concurrent_patch(
    tmp_path, monkeypatch
):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=1)
        submit = app.runtime.research.submit

        def patch_then_submit(owner_id, brief, **kwargs):
            response = app.client.patch(
                f"/watchlist/{item.id}",
                json={"revision": item.revision, "brief": f"{BRIEF} Updated scope."},
            )
            assert response.status_code == 200, response.text
            return submit(owner_id, brief, **kwargs)

        monkeypatch.setattr(app.runtime.research, "submit", patch_then_submit)
        assert tick(app, clock, item) is None
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)
        assert app.runtime.repos.watchlists.get("alice", item.id).revision == 2


def test_shutdown_between_tick_and_submit_creates_no_run(tmp_path, monkeypatch):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=1)
        submit = app.runtime.research.submit

        def stop_then_submit(owner_id, brief, **kwargs):
            app.runtime.begin_shutdown()
            return submit(owner_id, brief, **kwargs)

        monkeypatch.setattr(app.runtime.research, "submit", stop_then_submit)
        assert tick(app, clock, item) is None
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)


def test_raising_limit_restarts_from_a_future_tick_not_stale_due_time(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=1)
        first = tick(app, clock, item)
        assert app.wait(first.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None

        clock.at += timedelta(hours=2)
        current = app.runtime.repos.watchlists.get("alice", item.id)
        response = app.client.patch(
            f"/watchlist/{item.id}",
            json={"revision": current.revision, "max_runs": 2},
        )
        assert response.status_code == 200, response.text
        due = app.runtime.repos.watchlists.get("alice", item.id).next_tick_at
        assert due == clock.at + timedelta(minutes=item.cadence_minutes)
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)).next_run_time == due
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (1, 1, 0)


def test_last_allowed_run_retries_temporary_baseline_failure_on_its_own_short_job(
    tmp_path, monkeypatch
):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=1, cadence_minutes=525600)
        schedules = app.runtime.watchlists.schedules
        initialize = schedules.initialize
        failed_once = False

        def flaky(owner_id, item_id):
            nonlocal failed_once
            candidates = app.runtime.repos.watchlists.baseline_candidates(
                owner_id, item_id, BASELINE_SEED_SCHEMA_VERSION
            )
            if candidates and not failed_once:
                failed_once = True
                raise sqlite3.OperationalError("database is locked")
            return initialize(owner_id, item_id)

        monkeypatch.setattr(schedules, "initialize", flaky)
        run = tick(app, clock, item)
        assert app.wait(run.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        assert failed_once
        assert app.runtime.repos.watchlists.baseline("alice", item.id) is None
        # The exhausted research interval is gone; the retry waits one short delay, not
        # one cadence (a year here).
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None
        retry = app.runtime.scheduler.get_job(baseline_job_id("alice", item.id))
        assert retry.next_run_time == clock.at + BASELINE_RETRY_DELAY

        schedules.retry_baseline("alice", item.id)
        baseline = app.runtime.repos.watchlists.baseline("alice", item.id)
        assert baseline is not None and baseline.initializing_run_id == run.id
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (1, 1, 0)


def test_real_apscheduler_fires_baseline_retry_after_temporary_failure(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime

    with App(tmp_path) as app:
        item = setup(app, max_runs=1, cadence_minutes=525600)
        schedules = app.runtime.watchlists.schedules
        schedules.baseline_retry_delay = timedelta(milliseconds=200)
        initialize = schedules.initialize
        failures = []

        def flaky(owner_id, item_id):
            if not failures:
                failures.append(owner_id)
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            return initialize(owner_id, item_id)

        monkeypatch.setattr(schedules, "initialize", flaky)
        item = set_due(app, item, datetime.now(UTC))
        run = schedules.tick("alice", item.id, item.revision)
        assert app.wait(run.id).state == RunState.COMPLETE
        wait_until(
            lambda: app.runtime.repos.watchlists.baseline("alice", item.id) is not None
        )
        assert failures
        baseline = app.runtime.repos.watchlists.baseline("alice", item.id)
        assert baseline.initializing_run_id == run.id
        wait_until(
            lambda: app.runtime.scheduler.get_job(baseline_job_id("alice", item.id))
            is None
        )


def test_deleting_an_item_cancels_its_pending_baseline_retry(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app)
        schedules = app.runtime.watchlists.schedules
        schedules._schedule_baseline_retry("alice", item.id)
        assert app.runtime.scheduler.get_job(baseline_job_id("alice", item.id))
        assert app.client.delete(f"/watchlist/{item.id}").status_code == 204
        assert app.runtime.scheduler.get_job(baseline_job_id("alice", item.id)) is None
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is None


def test_unusable_source_rejection_skips_the_tick_without_raising(
    tmp_path, monkeypatch
):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app)

        def no_source(*_, **__):
            raise SubmissionRejected("web search is not available")

        monkeypatch.setattr(app.runtime.research, "submit", no_source)
        assert tick(app, clock, item) is None
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)
        assert app.runtime.scheduler.get_job(job_id("alice", item.id)) is not None


def test_tick_does_not_reread_baseline(tmp_path, monkeypatch):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=2)
        schedules = app.runtime.watchlists.schedules
        first = tick(app, clock, item)
        assert app.wait(first.id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        callers = []
        monkeypatch.setattr(
            schedules,
            "initialize",
            lambda *_: callers.append(threading.current_thread()),
        )
        second = tick(app, clock, item)
        assert app.wait(second.id).state == RunState.COMPLETE
        # Only the worker's completion step may initialize; the tick itself never does.
        assert threading.current_thread() not in callers


def test_patch_while_a_tick_is_due_keeps_that_tick(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=1)
        clock.at = item.next_tick_at + timedelta(seconds=30)
        response = app.client.patch(
            f"/watchlist/{item.id}", json={"revision": item.revision, "max_runs": 5}
        )
        assert response.status_code == 200, response.text
        # The tick fired for the old revision is stale, but the edit kept the due time,
        # so the re-synced job fires at once with the new revision and is admitted.
        assert (
            app.runtime.watchlists.schedules.tick("alice", item.id, item.revision)
            is None
        )
        current = app.runtime.repos.watchlists.get("alice", item.id)
        assert current.next_tick_at == item.next_tick_at
        job = app.runtime.scheduler.get_job(job_id("alice", item.id))
        assert job.next_run_time == item.next_tick_at
        run = app.runtime.watchlists.schedules.tick(
            "alice", item.id, current.revision
        )
        assert run is not None
        assert app.wait(run.id).state == RunState.COMPLETE


def test_stale_sync_cannot_remove_schedule_restored_by_concurrent_limit_raise(
    tmp_path, monkeypatch
):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, max_runs=1)
        schedules = app.runtime.watchlists.schedules
        sync = schedules.sync
        raised = []

        def raise_limit_then_sync(stale):
            # The tick's cleanup read the row while max_runs was 1; a PATCH raising the
            # limit (and restoring the job) lands before that stale snapshot is synced.
            if not raised and stale.max_runs == 1:
                raised.append(stale)
                response = app.client.patch(
                    f"/watchlist/{item.id}",
                    json={"revision": stale.revision, "max_runs": 2},
                )
                assert response.status_code == 200, response.text
                assert app.runtime.scheduler.get_job(job_id("alice", item.id))
            return sync(stale)

        monkeypatch.setattr(schedules, "sync", raise_limit_then_sync)
        run = tick(app, clock, item)
        assert raised and raised[0].max_runs == 1
        assert app.wait(run.id).state == RunState.COMPLETE
        current = app.runtime.repos.watchlists.get("alice", item.id)
        assert current.max_runs == 2
        job = app.runtime.scheduler.get_job(job_id("alice", item.id))
        assert job is not None and job.args[2] == current.revision
