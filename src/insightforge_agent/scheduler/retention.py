"""The retention sweep on the existing APScheduler (design.md section 6, Retention).

One persisted interval job (`retention-sweep`, daily) in the application's job store calls the
installed handler (`RetentionSweeper.sweep`). The job is added once and then kept across
restarts, so a restart never postpones it; a run missed while the process was down fires once
after startup (coalesced). There is no other daemon, queue or lock.

A sweep, with its cutoff fixed at its start (`now - retention_days`):

1. Backfills a failure record for every FAILED Run that has none (failed before records were
   kept); its retention starts now.
2. Lists the expired terminal Runs of every User, then, per User and in bounded batches:
   plans the next unprotected material (read only), removes the uploaded-document chunks of
   that batch from `doc_chunks`, then deletes from SQLite what is still eligible and
   unprotected in one transaction (`SqliteRetentionRepository`).

Crash and failure behaviour: a Qdrant failure keeps those chunks' Passages (they are skipped
for the rest of the sweep and retried by the next one); a failure after Qdrant and before or
during the SQLite transaction leaves SQLite unchanged, and the next sweep plans and removes the
same material again (a missing point is not an error). Re-running a sweep with nothing newly
expired changes nothing. A User whose retained records cannot be read is skipped entirely."""

import logging
import threading
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.domain.retention import (
    DEFAULT_RETENTION_DAYS,
    MalformedRetentionRoot,
    retention_cutoff,
)
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.retention import RetentionBatch

logger = logging.getLogger(__name__)

JOB_ID = "retention-sweep"
SWEEP_INTERVAL = timedelta(days=1)
FIRST_SWEEP_DELAY = timedelta(minutes=10)  # after the first start, clear of startup work
BATCH_PASSAGES = 100
_handler: Callable[[], object] | None = None


def install_retention_handler(handler: Callable[[], object] | None) -> None:
    global _handler
    _handler = handler


def fire_retention() -> None:
    """The persisted job's target."""
    if _handler is None:
        logger.error("retention sweep fired with no handler")
        return
    _handler()


def schedule_retention(scheduler, now: datetime, interval: timedelta = SWEEP_INTERVAL,
                       first_delay: timedelta = FIRST_SWEEP_DELAY) -> None:
    """Add the daily job unless the job store already holds it (kept as it is, so restarts
    never push it back). Call with the scheduler started (paused is enough)."""
    if scheduler.get_job(JOB_ID) is not None:
        return
    scheduler.add_job(f"{__name__}:fire_retention", trigger="interval",
                      seconds=interval.total_seconds(), next_run_time=now + first_delay,
                      id=JOB_ID, coalesce=True, max_instances=1, misfire_grace_time=None)


class RetentionSweeper:
    def __init__(self, repos, doc_index: DocChunkIndex | None, now: Callable[[], datetime], *,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 batch_passages: int = BATCH_PASSAGES, max_batches: int = 10_000,
                 closing: threading.Event | None = None) -> None:
        retention_cutoff(now(), retention_days)  # rejects a period that is not positive
        self.repos, self.doc_index, self.now = repos, doc_index, now
        self.retention_days, self.batch_passages = retention_days, batch_passages
        self.max_batches = max_batches
        self.closing = closing if closing is not None else threading.Event()
        self._running = threading.Lock()

    def sweep(self) -> Counter:
        """One sweep. Returns what it removed and skipped. Never raises for one User's
        problem; a second concurrent call returns at once."""
        done: Counter = Counter()
        if not self._running.acquire(blocking=False):
            done["already_running"] += 1
            return done
        try:
            now = self.now()
            cutoff = retention_cutoff(now, self.retention_days)
            store = self.repos.retention
            done["failures_backfilled"] += store.backfill_failures(now)
            by_owner: dict[str, list[str]] = defaultdict(list)
            for owner_id, run_id in store.expired_runs(cutoff):
                by_owner[owner_id].append(run_id)
            for owner_id, run_ids in sorted(by_owner.items()):
                if self.closing.is_set():
                    done["stopped_for_shutdown"] += 1
                    break
                try:
                    self._sweep_owner(owner_id, run_ids, cutoff, done)
                except MalformedRetentionRoot as error:
                    logger.error("retention skipped %s: %s", owner_id, error)
                    done["owners_skipped_malformed"] += 1
                except Exception:  # noqa: BLE001 - the next sweep retries; others go on
                    logger.exception("retention failed for %s", owner_id)
                    done["owners_failed"] += 1
            logger.info("retention sweep (cutoff %s): %s", cutoff.isoformat(), dict(done))
            return done
        finally:
            self._running.release()

    def _sweep_owner(self, owner_id: str, run_ids: list[str], cutoff: datetime,
                     done: Counter) -> None:
        store, skip = self.repos.retention, set()
        for _ in range(self.max_batches):
            if self.closing.is_set():
                return
            batch = store.plan(owner_id, run_ids, self.batch_passages, skip)
            if batch.empty:
                return
            failed = self._remove_chunks(batch, done)
            skip |= failed
            batch = batch.without(failed)
            applied = store.apply(owner_id, batch, cutoff)
            for name, value in vars(applied).items():
                done[name] += value
            if not applied.changed and not failed:
                return  # nothing planned was still removable: no progress is possible

    def _remove_chunks(self, batch: RetentionBatch, done: Counter) -> set[str]:
        """Remove the batch's uploaded-document chunks from `doc_chunks`, per fetching Run.
        Returns the Observations whose chunks could not be removed; their Passages stay."""
        by_run: dict[str, list] = defaultdict(list)
        for plan in batch.observations:
            if plan.document and plan.indices:
                by_run[plan.run_id].append(plan)
        failed: set[str] = set()
        for run_id, plans in by_run.items():
            ids = {p.observation_id for p in plans}
            if self.doc_index is None:  # chunks may exist but cannot be removed: keep them
                failed |= ids
                done["chunks_kept_no_index"] += sum(len(p.indices) for p in plans)
                continue
            refs = [PassageRef(observation_id=p.observation_id, index=i)
                    for p in plans for i in p.indices]
            try:
                self.doc_index.delete(batch.owner_id, run_id, refs)
            except Exception as error:  # noqa: BLE001 - Qdrant unavailable: keep the Passages
                logger.warning("doc_chunks removal failed for %s/%s: %s",
                               batch.owner_id, run_id, error)
                failed |= ids
                done["chunk_removal_failed"] += len(refs)
            else:
                done["chunk_points_removed"] += len(refs)
        return failed
