"""Slack-format webhook delivery of Alerts, on persisted APScheduler jobs (design.md section 8).

Delivery state lives in `alert_deliveries`, apart from the Alert, and is written in the same
transaction as the Alert. Each POST is counted durably before it is sent; a failure retries
with exponential backoff until `max_attempts`, then the delivery is failed permanently. Nothing
here can raise into the Diff or the Run. Delivery is at least once: a crash after a successful
POST and before recording it sends the Alert again."""

import logging
import threading
from collections.abc import Callable
from datetime import timedelta

import httpx

from insightforge_agent.domain.alerts import Alert

logger = logging.getLogger(__name__)
_handler: Callable | None = None
MAX_ATTEMPTS = 5
BASE_DELAY = timedelta(seconds=30)  # then 2, 8 and 32 minutes
POST_TIMEOUT_SECONDS = 10.0


def install_webhook_handler(handler):
    global _handler
    _handler = handler


def fire_webhook(owner_id, alert_id):
    if _handler is None:
        logger.error("Webhook delivery fired with no handler: %s/%s", owner_id, alert_id)
        return
    _handler(owner_id, alert_id)


def webhook_job_id(owner_id, alert_id):
    return f"alert-webhook:{owner_id}:{alert_id}"


_LABEL = {"added": "New", "changed": "Changed", "no_longer_true": "No longer true"}


def slack_payload(alert: Alert) -> dict:
    """Slack incoming-webhook format: `text` as the fallback, `blocks` for the layout."""
    n = len(alert.changes)
    title = f"InsightForge: {n} material change{'s' if n != 1 else ''} for \"{alert.brief}\""
    lines = []
    for c in alert.changes:
        value = c.object if c.polarity == "affirmed" else f"not {c.object}"
        before = f" (was {c.previous_object}, Report {c.previous_report_id})" \
            if c.previous_object is not None and c.outcome == "changed" else ""
        scope = f" [{c.scope}]" if c.scope else ""
        lines.append(f"• *{_LABEL[c.outcome]}*: {c.subject} {c.predicate}{scope}: "
                     f"{value}{before}. {c.when}.")
    footer = f"New Report {alert.report_id} (Run {alert.run_id})."
    if alert.failed_diffs:
        runs = ", ".join(f.run_id for f in alert.failed_diffs)
        footer += (f" Diffs failed for Runs {runs}; this is the net change since the last "
                   "successful comparison.")
    return {
        "text": f"{title}\n" + "\n".join(lines) + f"\n{footer}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:3000]}]},
        ],
    }


def post_json(url: str, payload: dict) -> None:
    response = httpx.post(url, json=payload, timeout=POST_TIMEOUT_SECONDS)
    response.raise_for_status()


class WebhookSender:
    def __init__(self, deps, scheduler, closing=None, post=post_json,
                 max_attempts=MAX_ATTEMPTS, base_delay=BASE_DELAY):
        self.deps, self.scheduler, self.post = deps, scheduler, post
        self.closing = closing if closing is not None else threading.Event()
        self.max_attempts, self.base_delay = max_attempts, base_delay

    def backoff(self, attempt: int) -> timedelta:
        return self.base_delay * (4 ** (attempt - 1))

    def schedule(self, owner_id, alert_id, at=None):
        """Never raises: startup recovery reschedules every pending delivery."""
        if self.closing.is_set() or self.scheduler is None:
            return
        try:
            self.scheduler.add_job(
                f"{__name__}:fire_webhook",
                trigger="date",
                run_date=at,  # None: now, on the scheduler's own clock
                args=[owner_id, alert_id],
                id=webhook_job_id(owner_id, alert_id),
                replace_existing=True,
                misfire_grace_time=None,
            )
        except Exception:
            logger.exception("Could not schedule webhook delivery %s/%s", owner_id, alert_id)

    def recover(self):
        try:
            pending = self.deps.repos.diffs.pending_deliveries()
        except Exception:
            logger.exception("Could not read pending webhook deliveries")
            return
        now = self.deps.now()
        for d in pending:
            self.schedule(d.owner_id, d.alert_id, max(d.next_attempt_at, now))

    def deliver(self, owner_id, alert_id):
        """The job callback. Never raises."""
        if self.closing.is_set():
            return  # startup recovery reschedules it
        try:
            self._deliver(owner_id, alert_id)
        except Exception:
            logger.exception("Webhook delivery %s/%s deferred", owner_id, alert_id)
            self.schedule(owner_id, alert_id, self.deps.now() + self.base_delay)

    def _deliver(self, owner_id, alert_id):
        repo = self.deps.repos.diffs
        attempt = repo.begin_delivery(owner_id, alert_id, self.max_attempts, self.deps.now())
        if attempt is None:
            return
        delivery = repo.delivery(owner_id, alert_id)
        alert = repo.alert(owner_id, alert_id)
        try:
            self.post(delivery.url, slack_payload(alert))
        except Exception as error:
            now = self.deps.now()
            retry_at = now + self.backoff(attempt) if attempt < self.max_attempts else None
            repo.record_delivery(owner_id, alert_id, delivered=False,
                                 error=f"{type(error).__name__}: {error}",
                                 next_attempt_at=retry_at, now=now)
            if retry_at is None:
                logger.warning("Webhook delivery %s/%s failed permanently after %s attempts",
                               owner_id, alert_id, attempt)
            else:
                logger.info("Webhook delivery %s/%s attempt %s failed; retry at %s",
                            owner_id, alert_id, attempt, retry_at)
                self.schedule(owner_id, alert_id, retry_at)
            return
        repo.record_delivery(owner_id, alert_id, delivered=True, error="",
                             next_attempt_at=None, now=self.deps.now())
