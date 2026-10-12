"""Persisted Watchlist intervals on the existing APScheduler, bounded research admission.

The durable due-time is consumed before admission. A refused admission skips that tick but
creates no Run; admission itself atomically checks revision, overlap and max_runs in SQLite.
"""

import logging
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
        self.closing = False

    def cancel(self, owner_id, item_id):
        try:
            self.scheduler.remove_job(job_id(owner_id, item_id))
        except JobLookupError:
            pass

    def sync(self, item):
        if self.closing:
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
            misfire_grace_time=1,
        )

    def recover(self):
        """While paused: repair missing jobs, remove deleted/exhausted ones, skip downtime."""
        repo = self.deps.repos.watchlists
        with self.deps.repos.lock:
            items = repo.list_all()
            live = {job_id(i.owner_id, i.id) for i in items}
            for job in self.scheduler.get_jobs():
                if job.id.startswith("watchlist:") and job.id not in live:
                    self.scheduler.remove_job(job.id)
            for item in items:
                self._initialize_quietly(item.owner_id, item.id)
                self.sync(repo.skip_missed(item.owner_id, item.id, self.deps.now()))

    def tick(self, owner_id, item_id, revision):
        with self.deps.repos.lock:
            if self.closing:
                return None
            repo = self.deps.repos.watchlists
            try:
                item = repo.take_tick(owner_id, item_id, revision, self.deps.now())
                if item is None:
                    return None
                # Recover a completion interrupted before Baseline initialization.
                self._initialize_quietly(owner_id, item_id)
                run = self.research.submit(
                    owner_id, item.as_brief(), watchlist_item_id=item_id
                )
                return run
            except (NotFoundError, WatchlistConflict, WorkNotAccepted):
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
            # Keep the first-success input unchanged; the next tick/startup retries it.
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
        with deps.repos.lock:
            existing = repo.baseline(owner_id, item_id)
            if existing is not None:
                return existing
            reports = [
                r
                for r in deps.repos.reports.list_for_owner(owner_id)
                if (run := deps.repos.runs.get(owner_id, r.run_id)).watchlist_item_id
                == item_id
                and run.state == RunState.COMPLETE
            ]
            if not reports:
                return None
            report = min(reports, key=lambda r: (r.created_at, r.id))
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
                    for p in refs:
                        deps.repos.passages.get(owner_id, p.observation_id, p.index)
                    seeds.append(
                        BaselineSeed(
                            claim=claim,
                            evidence=item,
                            observation_dates={
                                o.id: o.fetched_at for o in observations
                            },
                            source_ids=sorted({o.source_id for o in observations}),
                            report_id=report.id,
                            run_id=report.run_id,
                        )
                    )
            return repo.initialize_baseline(
                Baseline(
                    owner_id=owner_id,
                    watchlist_item_id=item_id,
                    initializing_run_id=report.run_id,
                    initialized_at=report.created_at,
                    seeds=seeds,
                )
            )
