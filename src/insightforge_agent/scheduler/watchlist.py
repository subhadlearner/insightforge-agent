"""Persisted Watchlist intervals on the existing APScheduler, bounded research admission.

The durable due-time is consumed before admission. A refused admission skips that tick but
creates no Run; admission itself atomically checks revision, overlap and max_runs in SQLite.
"""

import logging
import threading
from collections.abc import Callable

from apscheduler.jobstores.base import JobLookupError

from insightforge_agent.domain.contracts import EvidenceBundle, VerifiedReport
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import RunState
from insightforge_agent.domain.watchlist import (
    Baseline,
    BaselineSeed,
    WatchlistConflict,
)
from insightforge_agent.pipeline.executor import WorkNotAccepted

logger = logging.getLogger(__name__)
_handler: Callable | None = None


def install_watchlist_handler(handler):
    global _handler
    _handler = handler


def fire_watchlist(owner_id, item_id, revision):
    if _handler is None:
        logger.error("Watchlist tick fired with no handler: %s/%s", owner_id, item_id)
        return
    _handler(owner_id, item_id, revision)


def job_id(owner_id, item_id):
    return f"watchlist:{owner_id}:{item_id}"


class WatchlistScheduler:
    def __init__(self, deps, scheduler, research):
        self.deps, self.scheduler, self.research = deps, scheduler, research
        self.closing = threading.Event()

    def cancel(self, owner_id, item_id):
        try:
            self.scheduler.remove_job(job_id(owner_id, item_id))
        except JobLookupError:
            pass

    def sync(self, item):
        if self.closing.is_set():
            return
        started = self.deps.repos.watchlists.counts(item.owner_id, item.id)[0]
        if item.max_runs is not None and started >= item.max_runs:
            self.cancel(item.owner_id, item.id)
            return
        self.scheduler.add_job(
            f"{__name__}:fire_watchlist",
            trigger="interval",
            minutes=item.cadence_minutes,
            start_date=item.next_tick_at,
            next_run_time=item.next_tick_at,
            args=[item.owner_id, item.id, item.revision],
            id=job_id(item.owner_id, item.id),
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            # The durable next_tick_at comparison is the one clock/decision authority.
            # Coalescing admits one callback after scheduler-thread delay; recovery below
            # advances over downtime before the scheduler is resumed.
            misfire_grace_time=None,
        )

    def recover(self):
        """While paused: repair missing jobs, remove deleted/exhausted ones, skip downtime."""
        repo = self.deps.repos.watchlists
        items = repo.list_all()
        live = {job_id(i.owner_id, i.id) for i in items}
        for job in self.scheduler.get_jobs():
            if job.id.startswith("watchlist:") and job.id not in live:
                self.scheduler.remove_job(job.id)
        for item in items:
            self._initialize_quietly(item.owner_id, item.id)
            try:
                current = repo.skip_missed(item.owner_id, item.id, self.deps.now())
            except NotFoundError:
                self.cancel(item.owner_id, item.id)
                continue
            if current.next_tick_at != item.next_tick_at:
                logger.info(
                    "Skipped missed Watchlist ticks during recovery: %s/%s (%s -> %s)",
                    item.owner_id,
                    item.id,
                    item.next_tick_at,
                    current.next_tick_at,
                )
            self.sync(current)

    def tick(self, owner_id, item_id, revision):
        # Capture callback time before any repository lock contention. The store consumes
        # this due time once and advances past older intervals in the same transaction.
        now = self.deps.now()
        if self.closing.is_set():
            logger.info("Skipped Watchlist tick during shutdown: %s/%s", owner_id, item_id)
            return None
        repo = self.deps.repos.watchlists
        try:
            item = repo.take_tick(owner_id, item_id, revision, now)
            if item is None:
                logger.info(
                    "Skipped stale, duplicate, early, or missed Watchlist tick: "
                    "%s/%s revision %s",
                    owner_id,
                    item_id,
                    revision,
                )
                return None
            if self.closing.is_set():
                logger.info(
                    "Skipped admitted Watchlist tick during shutdown: %s/%s",
                    owner_id,
                    item_id,
                )
                return None
            return self.research.submit(
                owner_id, item.as_brief(), watchlist_item_id=item_id
            )
        except (NotFoundError, WatchlistConflict, WorkNotAccepted) as error:
            logger.info("Skipped Watchlist tick %s/%s: %s", owner_id, item_id, error)
            return None  # stale/deleted, overlap, limit or capacity: tick skipped
        finally:
            try:
                current = repo.get(owner_id, item_id)
                if (
                    current.max_runs is not None
                    and repo.counts(owner_id, item_id)[0] >= current.max_runs
                ):
                    self.cancel(owner_id, item_id)
            except NotFoundError:
                self.cancel(owner_id, item_id)

    def completed(self, run):
        """Outside the research graph, including after clarification resumption/recovery."""
        if run.state == RunState.COMPLETE and run.watchlist_item_id:
            self._initialize_quietly(run.owner_id, run.watchlist_item_id)

    def _initialize_quietly(self, owner_id, item_id):
        try:
            return self.initialize(owner_id, item_id)
        except NotFoundError:
            return None  # deletion does not cancel an already admitted Run
        except Exception:
            # Post-processing failure must not disable future research or process startup.
            # Keep the first-success input unchanged; completion/startup can retry it.
            logger.exception(
                "Baseline initialization deferred for %s/%s", owner_id, item_id
            )
            return None

    def initialize(self, owner_id, item_id):
        """Persist the earliest successful Run's current Claim/Evidence state exactly once.
        T12 replaces these seeds with normalized Findings, then owns comparison and updates.
        A failed Run is never a candidate. This operation creates no Diff or Alert.
        """
        deps, repo = self.deps, self.deps.repos.watchlists
        existing = repo.baseline(owner_id, item_id)
        if existing is not None:
            return existing
        for report in repo.baseline_candidates(owner_id, item_id):
            try:
                baseline = self._baseline_from_report(owner_id, item_id, report)
            except (KeyError, NotFoundError, ValueError) as error:
                first = repo.reject_baseline_candidate(
                    owner_id,
                    item_id,
                    report.run_id,
                    str(error),
                    deps.now(),
                )
                if first:
                    logger.warning(
                        "Rejected corrupt Baseline candidate %s for %s/%s: %s",
                        report.run_id,
                        owner_id,
                        item_id,
                        error,
                    )
                continue
            return repo.initialize_baseline(baseline)
        return None

    def _baseline_from_report(self, owner_id, item_id, report):
        deps = self.deps
        verified = VerifiedReport.model_validate(report.body["verified"])
        evidence = EvidenceBundle.model_validate(report.body["evidence"])
        items = {i.id: i for section in evidence.sections for i in section.items}
        rejected = {
            (v.section, v.claim)
            for v in verified.verdicts
            if v.verdict == "unverified"
        }
        seeds = []
        for si, section in enumerate(verified.draft.sections):
            for ci, claim in enumerate(section.claims):
                if claim.historical or (si, ci) in rejected:
                    continue
                item = items[claim.evidence_id]
                refs = list(dict.fromkeys([item.primary, *item.supporting]))
                observations = [
                    deps.repos.observations.get(owner_id, p.observation_id)
                    for p in refs
                ]
                if not observations or any(
                    o.run_id != report.run_id for o in observations
                ):
                    raise ValueError(
                        "Baseline Evidence must be fetched in the establishing Run"
                    )
                for observation in observations:
                    source = deps.repos.sources.get(owner_id, observation.source_id)
                    if source.kind != "web":
                        raise ValueError(
                            "Watchlist Baseline Evidence must be web-sourced"
                        )
                for passage in refs:
                    deps.repos.passages.get(
                        owner_id, passage.observation_id, passage.index
                    )
                seeds.append(
                    BaselineSeed(
                        claim=claim,
                        evidence=item,
                        observation_dates={o.id: o.fetched_at for o in observations},
                        source_ids=sorted({o.source_id for o in observations}),
                        report_id=report.id,
                        run_id=report.run_id,
                    )
                )
        return Baseline(
            owner_id=owner_id,
            watchlist_item_id=item_id,
            initializing_run_id=report.run_id,
            initialized_at=report.created_at,
            seeds=seeds,
        )
