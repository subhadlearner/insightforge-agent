"""Durable clarification timeouts on APScheduler (design.md section 2, Timeout job).

Each Run awaiting an answer has one one-shot `date` job at its deadline in the SQLAlchemy job
store, in the application database, so the obligation survives a restart. The job is stored by
reference (`fire_timeout` with the owner and Run IDs), never as an in-memory callback; at
startup the application installs the handler (`ClarificationService.time_out`) it calls.

APScheduler 3 writes a job to its store only once the scheduler has started (paused is enough),
so start it before the first Run. The Run's stored deadline stays the record: the startup
sweep times out or reschedules every awaiting Run from it, so a job that never reached the
store (a crash between storing the question and scheduling) strands nothing.

While the process runs, one persisted interval job (`clarification-reconcile`, every minute)
calls the installed reconciliation handler (`ClarificationService.reconcile_overdue`), which
times out every Run still awaiting an answer past its stored deadline through the same
compare-and-set. So a timeout job that failed to reach the store is made good within about a
minute of the deadline, without a restart (T20 B4, T9-1). It needs the scheduler itself: with
the scheduler down, only the next startup sweep times such a Run out."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler

logger = logging.getLogger(__name__)

JOB_TABLE = "apscheduler_jobs"
JOBSTORE = "default"  # the one job store, in the application database
RECONCILE_JOB_ID = "clarification-reconcile"
RECONCILE_INTERVAL = timedelta(minutes=1)
_handler: Callable[[str, str], object] | None = None
_reconcile_handler: Callable[[], object] | None = None


def install_timeout_handler(handler: Callable[[str, str], object] | None) -> None:
    """What a fired timeout job calls with (owner_id, run_id)."""
    global _handler
    _handler = handler


def fire_timeout(owner_id: str, run_id: str) -> None:
    """The persisted job's target. Without an installed handler the job is logged and lost;
    the startup sweep still times the Run out from its stored deadline."""
    if _handler is None:
        logger.error("clarification timeout for %s/%s fired with no handler", owner_id, run_id)
        return
    _handler(owner_id, run_id)


def install_reconcile_handler(handler: Callable[[], object] | None) -> None:
    """What the reconciliation job calls."""
    global _reconcile_handler
    _reconcile_handler = handler


def fire_reconcile() -> None:
    """The persisted reconciliation job's target. Without a handler it is logged; the next
    run, or the startup sweep, still times overdue Runs out."""
    if _reconcile_handler is None:
        logger.error("clarification reconciliation fired with no handler")
        return
    _reconcile_handler()


def schedule_reconciliation(scheduler: BaseScheduler, now: datetime,
                            interval: timedelta = RECONCILE_INTERVAL) -> None:
    """Add the interval job unless the job store already holds it (kept as it is, so a restart
    never postpones it). Call with the scheduler started (paused is enough)."""
    if scheduler.get_job(RECONCILE_JOB_ID) is not None:
        return
    scheduler.add_job(f"{__name__}:fire_reconcile", trigger="interval",
                      seconds=interval.total_seconds(), next_run_time=now + interval,
                      id=RECONCILE_JOB_ID, coalesce=True, max_instances=1,
                      misfire_grace_time=None)


def job_id(owner_id: str, run_id: str) -> str:
    return f"clarification-timeout:{owner_id}:{run_id}"


def build_scheduler(database_url: str) -> BackgroundScheduler:
    """A scheduler whose job store is the application database (`sqlite:///...`)."""
    return BackgroundScheduler(
        jobstores={JOBSTORE: SQLAlchemyJobStore(url=database_url, tablename=JOB_TABLE)},
        timezone=UTC)


class ApsTimeoutScheduler:
    """`TimeoutScheduler` on an APScheduler scheduler."""

    def __init__(self, scheduler: BaseScheduler) -> None:
        self._scheduler = scheduler

    def schedule(self, owner_id: str, run_id: str, at: datetime) -> None:
        # Replacing keeps one job per Run, however often the sweep reschedules it. A job whose
        # time passed while the process was down still runs once (no misfire limit).
        self._scheduler.add_job(
            f"{__name__}:fire_timeout", trigger="date", run_date=at, args=[owner_id, run_id],
            id=job_id(owner_id, run_id), replace_existing=True, misfire_grace_time=None,
            coalesce=True)

    def cancel(self, owner_id: str, run_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id(owner_id, run_id))
        except JobLookupError:
            pass  # already fired, or never stored

    def scheduled_at(self, owner_id: str, run_id: str) -> datetime | None:
        job = self._scheduler.get_job(job_id(owner_id, run_id))
        return job.next_run_time if job else None
