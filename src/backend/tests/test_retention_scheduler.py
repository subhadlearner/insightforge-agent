"""T13: the daily retention job on the real APScheduler and SQLAlchemy job store, and its
registration in the application's startup and shutdown."""

import logging
from datetime import UTC, datetime, timedelta

from insightforge_agent.scheduler import retention as retention_module
from insightforge_agent.scheduler.clarification import build_scheduler
from insightforge_agent.scheduler.retention import (
    FIRST_SWEEP_DELAY,
    JOB_ID,
    fire_retention,
    install_retention_handler,
    schedule_retention,
)
from tests.api.support import App

NOW = datetime(2099, 1, 1, tzinfo=UTC)  # never due while a test runs


def scheduler_over(tmp_path):
    scheduler = build_scheduler(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    scheduler.start(paused=True)
    return scheduler


def test_the_daily_job_is_stored_once_and_a_restart_never_postpones_it(tmp_path):
    first = scheduler_over(tmp_path)
    schedule_retention(first, NOW)
    job = first.get_job(JOB_ID)
    assert job.next_run_time == NOW + FIRST_SWEEP_DELAY
    assert job.trigger.interval == timedelta(days=1)
    assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time is None
    first.shutdown(wait=True)

    second = scheduler_over(tmp_path)  # a new process, a day later
    schedule_retention(second, NOW + timedelta(days=1))
    assert second.get_job(JOB_ID).next_run_time == NOW + FIRST_SWEEP_DELAY
    assert [j.id for j in second.get_jobs()] == [JOB_ID]
    second.shutdown(wait=True)


def test_the_job_calls_the_installed_handler_and_only_logs_without_one(caplog):
    calls = []
    install_retention_handler(lambda: calls.append(1))
    try:
        fire_retention()
    finally:
        install_retention_handler(None)
    assert calls == [1]
    with caplog.at_level(logging.ERROR):
        fire_retention()
    assert "no handler" in caplog.text


def test_the_app_registers_the_job_and_handler_and_removes_the_handler_on_stop(tmp_path):
    with App(tmp_path, retention_days=7) as app:
        runtime = app.runtime
        assert runtime.scheduler.get_job(JOB_ID) is not None
        assert retention_module._handler == runtime.retention.sweep
        assert runtime.retention.retention_days == 7
        assert runtime.retention.doc_index is runtime.deps.doc_index
        assert runtime.retention.sweep()["already_running"] == 0
    assert retention_module._handler is None
    assert runtime.retention.closing.is_set()  # a sweep in progress stops between batches
