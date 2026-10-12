from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

from insightforge_agent.domain.models import RunState
from insightforge_agent.scheduler.watchlist import job_id
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


def test_ticks_overlap_limit_counts_baseline_and_reports_survive_delete(tmp_path):
    clock = Clock(T0)
    gate = Gate(fakes.FixtureSearch(), open_=False)
    with App(tmp_path, gate=gate, now=clock) as app:
        item = setup(app, cadence_minutes=1, max_runs=2)
        schedules = app.runtime.watchlists.schedules
        job = app.runtime.scheduler.get_job(job_id("alice", item.id))
        assert job.max_instances == 1 and job.coalesce and job.misfire_grace_time == 1
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
                f"/watchlist/{item.id}", json={"brief": "Different research"}
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
                f"/watchlist/{item.id}", json={"cadence_minutes": 30}
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
        item = item.model_copy(update={"next_tick_at": due})
        app.runtime.repos.watchlists.update(item, item.revision)
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


def test_real_apscheduler_discards_misfire_without_run_or_counter(tmp_path):
    from datetime import UTC, datetime
    from apscheduler.events import EVENT_JOB_MISSED

    with App(tmp_path) as app:
        item = setup(app, max_runs=1)
        scheduler = app.runtime.scheduler
        scheduler.pause()
        missed = threading.Event()
        scheduler.add_listener(lambda event: missed.set(), EVENT_JOB_MISSED)
        item = item.model_copy(
            update={"next_tick_at": datetime.now(UTC) - timedelta(seconds=10)}
        )
        app.runtime.repos.watchlists.update(item, item.revision)
        app.runtime.watchlists.schedules.sync(item)
        scheduler.resume()
        assert missed.wait(10)
        assert app.runtime.repos.watchlists.counts("alice", item.id) == (0, 0, 0)
        assert app.runtime.repos.runs.list_for_owner("alice") == []
        assert scheduler.get_job(job_id("alice", item.id)).next_run_time > datetime.now(
            UTC
        )


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
