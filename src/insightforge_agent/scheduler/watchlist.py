"""Persisted Watchlist intervals on the existing APScheduler, bounded research admission.

The durable due-time is consumed before admission. A refused admission skips that tick but
creates no Run; admission itself atomically checks revision, overlap and max_runs in SQLite.
A temporary Baseline failure gets its own short one-shot retry job, independent of the
research interval, so an exhausted item never keeps a research schedule only to retry it.
"""

import logging
import threading
from collections.abc import Callable
from datetime import timedelta

from apscheduler.jobstores.base import JobLookupError
from pydantic import ValidationError

from insightforge_agent.domain.contracts import EvidenceBundle, VerifiedReport
from insightforge_agent.domain.errors import NotFoundError, SubmissionRejected
from insightforge_agent.domain.models import RunState
from insightforge_agent.domain.watchlist import (
    BASELINE_SEED_SCHEMA_VERSION,
    Baseline,
    BaselineSeed,
    InvalidBaselineCandidate,
    WatchlistConflict,
)
from insightforge_agent.pipeline.executor import WorkNotAccepted

logger = logging.getLogger(__name__)
_handler: Callable | None = None
_baseline_handler: Callable | None = None
BASELINE_RETRY_DELAY = timedelta(minutes=1)


def install_watchlist_handler(handler, baseline_handler=None):
    global _handler, _baseline_handler
    _handler, _baseline_handler = handler, baseline_handler


def fire_watchlist(owner_id, item_id, revision):
    if _handler is None:
        logger.error("Watchlist tick fired with no handler: %s/%s", owner_id, item_id)
        return
    _handler(owner_id, item_id, revision)


def fire_baseline_retry(owner_id, item_id):
    if _baseline_handler is None:
        logger.error("Baseline retry fired with no handler: %s/%s", owner_id, item_id)
        return
    _baseline_handler(owner_id, item_id)


def job_id(owner_id, item_id):
    return f"watchlist:{owner_id}:{item_id}"


def baseline_job_id(owner_id, item_id):
    return f"watchlist-baseline:{owner_id}:{item_id}"


def diff_job_id(owner_id, item_id):
    """The T12 Diff job (scheduler/diff.py); listed here so recovery and cancel own it."""
    return f"watchlist-diff:{owner_id}:{item_id}"


class WatchlistScheduler:
    def __init__(
        self,
        deps,
        scheduler,
        research,
        closing=None,
        baseline_retry_delay=BASELINE_RETRY_DELAY,
        diffs=None,
    ):
        self.deps, self.scheduler, self.research = deps, scheduler, research
        self.closing = closing if closing is not None else threading.Event()
        self.baseline_retry_delay = baseline_retry_delay
        self.diffs = diffs  # T12 DiffService; None leaves T11 behaviour unchanged

    def _job_ids(self, owner_id, item_id):
        return (
            job_id(owner_id, item_id),
            baseline_job_id(owner_id, item_id),
            diff_job_id(owner_id, item_id),
        )

    def cancel(self, owner_id, item_id):
        for job in self._job_ids(owner_id, item_id):
            self._remove(job)

    def _remove(self, job):
        try:
            self.scheduler.remove_job(job)
        except JobLookupError:
            pass

    def sync(self, item):
        """Schedule from the current row, never the caller's snapshot. The repository lock
        is the one PATCH holds around its update and sync, so a stale caller (a tick's
        cleanup) cannot undo a concurrent limit raise."""
        if self.closing.is_set():
            return
        repo = self.deps.repos.watchlists
        with self.deps.repos.lock:
            try:
                item = repo.get(item.owner_id, item.id)
            except NotFoundError:
                self.cancel(item.owner_id, item.id)
                return
            started = repo.counts(item.owner_id, item.id)[0]
            if item.max_runs is not None and started >= item.max_runs:
                self._remove(job_id(item.owner_id, item.id))
                return
            self._add_interval(item)

    def _add_interval(self, item):
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
        live = {job for i in items for job in self._job_ids(i.owner_id, i.id)}
        for job in self.scheduler.get_jobs():
            if job.id.startswith("watchlist") and job.id not in live:
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
            self._diff_soon(item.owner_id, item.id)

    def _diff_soon(self, owner_id, item_id):
        """The Diff job normalises the seed, then diffs every unfinished COMPLETE Run in
        order; with nothing to do it returns at once."""
        if self.diffs is not None:
            self.diffs.schedule(owner_id, item_id)

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
                owner_id,
                item.as_brief(),
                watchlist_item_id=item_id,
                watchlist_revision=item.revision,
                admission_open=lambda: not self.closing.is_set(),
            )
        except (
            NotFoundError,
            WatchlistConflict,
            WorkNotAccepted,
            SubmissionRejected,
        ) as error:
            logger.info("Skipped Watchlist tick %s/%s: %s", owner_id, item_id, error)
            # stale/deleted, overlap, limit, capacity, or no usable source: tick skipped
            return None
        finally:
            try:
                current = repo.get(owner_id, item_id)
                if (
                    current.max_runs is not None
                    and repo.counts(owner_id, item_id)[0] >= current.max_runs
                ):
                    self.sync(current)
            except NotFoundError:
                self.cancel(owner_id, item_id)

    def completed(self, run):
        """Outside the research graph, including after clarification resumption/recovery."""
        if run.state == RunState.COMPLETE and run.watchlist_item_id:
            self._initialize_quietly(run.owner_id, run.watchlist_item_id)
            self._diff_soon(run.owner_id, run.watchlist_item_id)

    def retry_baseline(self, owner_id, item_id):
        """The one-shot retry job's callback; it reschedules itself while still failing."""
        if self.closing.is_set():
            return  # startup recovery retries every item
        if self._initialize_quietly(owner_id, item_id) is not None:
            self._diff_soon(owner_id, item_id)

    def _initialize_quietly(self, owner_id, item_id):
        try:
            return self.initialize(owner_id, item_id)
        except NotFoundError:
            return None  # deletion does not cancel an already admitted Run
        except Exception:
            # Post-processing failure must not disable future research or process startup.
            # Keep the first-success input unchanged and retry it shortly on its own job.
            logger.exception(
                "Baseline initialization deferred for %s/%s", owner_id, item_id
            )
            self._schedule_baseline_retry(owner_id, item_id)
            return None

    def _schedule_baseline_retry(self, owner_id, item_id):
        if self.closing.is_set() or self.scheduler is None:
            return
        try:
            self.scheduler.add_job(
                f"{__name__}:fire_baseline_retry",
                trigger="date",
                run_date=self.deps.now() + self.baseline_retry_delay,
                args=[owner_id, item_id],
                id=baseline_job_id(owner_id, item_id),
                replace_existing=True,
                misfire_grace_time=None,
            )
        except Exception:
            # Startup recovery still retries every item that has no Baseline.
            logger.exception(
                "Could not schedule Baseline retry for %s/%s", owner_id, item_id
            )

    def initialize(self, owner_id, item_id):
        """Persist the earliest successful Run's current Claim/Evidence state exactly once.
        T12 replaces these seeds with normalized Findings, then owns comparison and updates.
        A failed Run is never a candidate. This operation creates no Diff or Alert.
        """
        deps, repo = self.deps, self.deps.repos.watchlists
        existing = repo.baseline(owner_id, item_id)
        if existing is not None:
            return existing
        for report in repo.baseline_candidates(
            owner_id, item_id, BASELINE_SEED_SCHEMA_VERSION
        ):
            try:
                baseline = self._baseline_from_report(owner_id, item_id, report)
            except InvalidBaselineCandidate as error:
                first = repo.reject_baseline_candidate(
                    owner_id,
                    item_id,
                    report.run_id,
                    str(error),
                    deps.now(),
                    BASELINE_SEED_SCHEMA_VERSION,
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
        return Baseline(
            owner_id=owner_id,
            watchlist_item_id=item_id,
            initializing_run_id=report.run_id,
            initialized_at=report.created_at,
            seeds=report_seeds(self.deps, owner_id, report),
        )


def report_seeds(deps, owner_id, report) -> list[BaselineSeed]:
    """A successful Report's current Claims with their structured Evidence and provenance.
    Used for the Baseline seed and, by the Diff, for every later Run: the same eligibility
    (current Claims only, never Historical or unverified ones; every supporting Observation
    fetched by this Run from a web Source). Raises InvalidBaselineCandidate only for defects of
    the Report's own stored data; anything else (a store outage, a bug) propagates and is
    retried."""
    body = report.body
    if not isinstance(body, dict) or not {"verified", "evidence"} <= body.keys():
        raise InvalidBaselineCandidate("Report body lacks verified Report and Evidence")
    try:
        verified = VerifiedReport.model_validate(body["verified"])
        evidence = EvidenceBundle.model_validate(body["evidence"])
    except ValidationError as error:
        raise InvalidBaselineCandidate(f"malformed Report body: {error}") from error
    items = {i.id: i for section in evidence.sections for i in section.items}

    def stored(kind, read, *key):
        try:
            return read(owner_id, *key)
        except NotFoundError as error:
            raise InvalidBaselineCandidate(f"missing {kind} {key[0]}") from error

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
            item = items.get(claim.evidence_id)
            if item is None:
                raise InvalidBaselineCandidate(
                    f"Claim cites missing Evidence {claim.evidence_id}"
                )
            refs = list(dict.fromkeys([item.primary, *item.supporting]))
            observations = [
                stored("Observation", deps.repos.observations.get, p.observation_id)
                for p in refs
            ]
            if not observations or any(
                o.run_id != report.run_id for o in observations
            ):
                raise InvalidBaselineCandidate(
                    "Watchlist Evidence must be fetched by the Report's own Run"
                )
            for observation in observations:
                source = stored(
                    "Source", deps.repos.sources.get, observation.source_id
                )
                if source.kind != "web":
                    raise InvalidBaselineCandidate(
                        "Watchlist Evidence must be web-sourced"
                    )
            for passage in refs:
                stored(
                    "Passage",
                    deps.repos.passages.get,
                    passage.observation_id,
                    passage.index,
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
    return seeds
