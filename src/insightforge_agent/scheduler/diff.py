"""The Diff Agent: per Watchlist Item, after COMPLETE, outside the research graph (section 8).

Work for an item is the ordered queue of its COMPLETE Runs after the initializing one whose
Diff is unfinished. A persisted APScheduler job (`watchlist-diff:<owner>:<item>`) processes it:

1. The T11 seed is normalised into established Findings first (idempotently), before any Diff.
   A failed normalisation is retried with a persisted backoff (`SEED_RETRY_DELAYS`: 1, 2, 4,
   8, 16, 32 minutes, then every 60 minutes, indefinitely), checked before the Finding judge
   is called, so a duplicate job, a Run's completion or a restart cannot bring it forward.
   Later Runs wait meanwhile; success clears it (T20 B4, T12-2).
2. Each Run in order: its current Claims are normalised (the same eligibility as the seed),
   compared with the established state, and the result, the Baseline update and any Alert are
   committed in one transaction (`SqliteDiffRepository.commit`).
3. A failed attempt is retried once, after `retry_delay`, and later Runs wait for it. A second
   failure is permanent: the Baseline is unchanged and the next Diff reports the net change.

Nothing here changes a Run's status. Every step that matters is decided by persistent
constraints; the per-item lock only avoids duplicate work inside this process.
"""

import logging
import threading
from collections.abc import Callable
from datetime import timedelta

from insightforge_agent.agents.finding_judge import FindingJudge
from insightforge_agent.domain.alerts import (
    MAX_DIFF_ATTEMPTS,
    Alert,
    WebhookDelivery,
    alert_change,
    alert_id,
)
from insightforge_agent.domain.diff import DEFAULT_UNCONFIRMED_AFTER, compare, establish
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.findings import DEFAULT_REGISTRY, PredicateRegistry
from insightforge_agent.pipeline.findings import normalize_seeds
from insightforge_agent.scheduler.watchlist import diff_job_id, report_seeds
from insightforge_agent.stores.diffs import DiffConflict

logger = logging.getLogger(__name__)
_handler: Callable | None = None
DIFF_RETRY_DELAY = timedelta(minutes=1)
# How long a started attempt holds its Diff against a second attempt (another connection, or a
# restart after a crash). Longer than a Diff takes: normalisation plus one bounded judge call.
DIFF_ATTEMPT_LEASE = timedelta(minutes=10)
# Waits after the 1st, 2nd, ... failed seed normalisation; the last repeats indefinitely.
SEED_RETRY_DELAYS = tuple(timedelta(minutes=m) for m in (1, 2, 4, 8, 16, 32, 60))


def seed_retry_delay(failures: int) -> timedelta:
    """The wait after the `failures`-th failure (1 or more)."""
    if failures < 1:
        raise ValueError("a seed retry delay follows at least one failure")
    return SEED_RETRY_DELAYS[min(failures, len(SEED_RETRY_DELAYS)) - 1]


def install_diff_handler(handler):
    global _handler
    _handler = handler


def fire_diff(owner_id, item_id):
    if _handler is None:
        logger.error("Diff fired with no handler: %s/%s", owner_id, item_id)
        return
    _handler(owner_id, item_id)


class DiffService:
    def __init__(
        self,
        deps,
        scheduler,
        closing=None,
        *,
        registry: PredicateRegistry = DEFAULT_REGISTRY,
        judge: FindingJudge | None = None,
        webhooks=None,
        webhook_url_for: Callable[[str], str | None] = lambda owner_id: None,
        unconfirmed_after: int = DEFAULT_UNCONFIRMED_AFTER,
        retry_delay: timedelta = DIFF_RETRY_DELAY,
        attempt_lease: timedelta = DIFF_ATTEMPT_LEASE,
    ):
        self.deps, self.scheduler = deps, scheduler
        self.closing = closing if closing is not None else threading.Event()
        self.registry = registry
        self.judge = judge if judge is not None else FindingJudge(deps.light_model, registry)
        self.webhooks, self.webhook_url_for = webhooks, webhook_url_for
        self.unconfirmed_after, self.retry_delay = unconfirmed_after, retry_delay
        self.attempt_lease = attempt_lease
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def schedule(self, owner_id, item_id, at=None):
        """Never raises: startup recovery schedules every item again. Bringing a job forward
        is harmless: `process` honours a failed Diff's stored `retry_after`."""
        if self.closing.is_set() or self.scheduler is None:
            return
        try:
            self.scheduler.add_job(
                f"{__name__}:fire_diff",
                trigger="date",
                # Immediate work (None) runs on the scheduler's own clock; a retry is
                # anchored to the application clock, like the T11 Baseline retry.
                run_date=at,
                args=[owner_id, item_id],
                id=diff_job_id(owner_id, item_id),
                replace_existing=True,
                misfire_grace_time=None,
            )
        except Exception:
            logger.exception("Could not schedule Diff for %s/%s", owner_id, item_id)

    def run_item(self, owner_id, item_id):
        """The job callback. Never raises into the scheduler or a Run."""
        if self.closing.is_set():
            return
        try:
            self.process(owner_id, item_id)
        except NotFoundError:
            return  # the item was deleted; its Runs and Reports remain
        except Exception:
            logger.exception("Diff processing deferred for %s/%s", owner_id, item_id)
            self.schedule(owner_id, item_id, self.deps.now() + self.retry_delay)

    def _lock(self, owner_id, item_id) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault((owner_id, item_id), threading.Lock())

    def process(self, owner_id, item_id):
        with self._lock(owner_id, item_id):
            if not self.ensure_established(owner_id, item_id):
                return
            repo = self.deps.repos.diffs
            while not self.closing.is_set():
                nxt = repo.next_run(owner_id, item_id)
                if nxt is None:
                    return
                run_id, report_id = nxt
                attempt, wait_until = repo.begin_attempt(
                    owner_id, item_id, run_id, report_id, MAX_DIFF_ATTEMPTS, self.deps.now(),
                    self.attempt_lease)
                if wait_until is not None:
                    # A retry not yet due, or another attempt in progress: neither this Run nor
                    # a later one may start before then.
                    self.schedule(owner_id, item_id, wait_until)
                    return
                if attempt is None:
                    continue  # already terminal (or attempts exhausted by a crash)
                try:
                    self._diff(owner_id, item_id, run_id, report_id)
                except NotFoundError:
                    raise
                except DiffConflict as error:
                    logger.info("Diff %s/%s/%s recomputed: %s", owner_id, item_id, run_id,
                                error)
                    repo.release_attempt(owner_id, item_id, run_id)
                except Exception as error:
                    now = self.deps.now()
                    retry_at = now + self.retry_delay if attempt < MAX_DIFF_ATTEMPTS else None
                    repo.record_failure(owner_id, item_id, run_id,
                                        f"{type(error).__name__}: {error}", retry_at, now)
                    if retry_at is not None:
                        logger.warning("Diff %s/%s/%s failed; retrying once at %s", owner_id,
                                       item_id, run_id, retry_at, exc_info=True)
                        self.schedule(owner_id, item_id, retry_at)
                        return  # later Runs wait for this one
                    logger.error("Diff %s/%s/%s failed permanently; Baseline unchanged",
                                 owner_id, item_id, run_id, exc_info=True)

    def ensure_established(self, owner_id, item_id) -> bool:
        """Normalise the T11 seed into established Findings, exactly once. False when there is
        no successful Run yet, or while a failed normalisation waits for its backoff; the Diff
        job is then scheduled at the backoff's end."""
        repo = self.deps.repos.diffs
        state = self.deps.repos.watchlists.baseline_state(owner_id, item_id)
        if state != "seed":
            return state == "established"
        now = self.deps.now()
        retry_after = repo.seed_retry_after(owner_id, item_id)
        if retry_after is not None and now < retry_after:
            self.schedule(owner_id, item_id, retry_after)  # no judge call before then
            return False
        baseline = self.deps.repos.watchlists.baseline(owner_id, item_id)
        try:
            normalized = normalize_seeds(baseline.seeds, baseline.initializing_run_id,
                                         self.registry, self.judge)
            result = establish(normalized.findings, self.registry)
            established = repo.establish(owner_id, item_id, baseline.initializing_run_id,
                                         result, normalized.skipped, now)
        except NotFoundError:
            raise  # the item was deleted
        except Exception as error:  # noqa: BLE001 - every failure backs off, none is final
            retry_after = repo.record_seed_failure(
                owner_id, item_id, f"{type(error).__name__}: {error}", now, seed_retry_delay)
            logger.warning("Baseline seed normalisation for %s/%s failed; retrying at %s",
                           owner_id, item_id, retry_after, exc_info=True)
            self.schedule(owner_id, item_id, retry_after)
            return False
        if established:
            logger.info("Baseline established for %s/%s: %s Findings, %s skipped", owner_id,
                        item_id, len(result.updates), len(normalized.skipped))
        return True

    def _diff(self, owner_id, item_id, run_id, report_id):
        deps, repo = self.deps, self.deps.repos.diffs
        if report_id is None:
            raise ValueError(f"COMPLETE Run {run_id} has no Report")
        report = deps.repos.reports.get(owner_id, report_id)
        normalized = normalize_seeds(report_seeds(deps, owner_id, report), run_id,
                                     self.registry, self.judge)
        version, established = repo.findings(owner_id, item_id)
        result = compare(established, normalized.findings, self.registry,
                         self.unconfirmed_after)
        intervening = repo.intervening_failures(owner_id, item_id, run_id)
        now = deps.now()
        alert = delivery = None
        if result.material:
            item = deps.repos.watchlists.get(owner_id, item_id)
            alert = Alert(id=alert_id(run_id), owner_id=owner_id, watchlist_item_id=item_id,
                          run_id=run_id, report_id=report_id, brief=item.brief,
                          created_at=now, changes=[alert_change(e) for e in result.material],
                          failed_diffs=intervening)
            url = self.webhook_url_for(owner_id)
            if url:
                delivery = WebhookDelivery(owner_id=owner_id, alert_id=alert.id, url=url,
                                           next_attempt_at=now)
        committed = repo.commit(owner_id, item_id, run_id, version, result,
                                normalized.skipped, intervening, alert, delivery, now)
        if not committed:
            return
        logger.info("Diff %s/%s/%s: %s material change(s)%s", owner_id, item_id, run_id,
                    len(result.material), f", Alert {alert.id}" if alert else "")
        if delivery is not None and self.webhooks is not None:
            self.webhooks.schedule(owner_id, alert.id)
