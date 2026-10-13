"""T12 CP4: Alerts to a Slack-format webhook, with bounded retry and backoff, isolated from
research and from the committed Diff. No real network: the POST is injected."""

from datetime import timedelta

import httpx
import pytest

from insightforge_agent.domain.models import RunState
from insightforge_agent.scheduler import webhook as webhook_module
from insightforge_agent.scheduler.webhook import (
    BASE_DELAY,
    MAX_ATTEMPTS,
    post_json,
    slack_payload,
    webhook_job_id,
)
from tests.stores.t12_world import ITEM, NOW, OWNER, World, fact

URL = "https://hooks.example.test/T000/B000/xyz"


class FlakyPost:
    def __init__(self, failures=0):
        self.failures, self.calls = failures, []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if len(self.calls) <= self.failures:
            raise httpx.ConnectError("connection refused")


def changed_world(tmp_path, post, url=URL):
    w = World(tmp_path / "hook.db", webhook_url=url, post=post)
    w.add_item()
    w.complete("r1", 1, fact(period="2030-01-01", derived=True))
    w.seed()
    w.complete("r2", 2, fact("$130", period="2030-01-02", derived=True))
    w.process()
    return w


def fire(world, alert_id):
    job = world.scheduler.jobs.pop(webhook_job_id(OWNER, alert_id))
    world.now = max(world.now, job["run_date"] or world.now)
    world.webhooks.deliver(*job["args"])
    return job


def test_alert_is_posted_in_slack_format_and_delivery_state_is_separate(tmp_path):
    post = FlakyPost()
    world = changed_world(tmp_path, post)
    [alert] = world.alerts()
    delivery = world.repos.diffs.delivery(OWNER, alert.id)
    assert (delivery.status, delivery.attempts, delivery.url) == ("pending", 0, URL)
    fire(world, alert.id)
    [(url, payload)] = post.calls
    assert url == URL
    assert "Change detected on 2030-01-02" in payload["text"]
    assert "Changed on" not in payload["text"]
    assert {b["type"] for b in payload["blocks"]} == {"header", "section", "context"}
    assert "rep_r2" in payload["text"] and "rep_r1" in payload["text"]
    delivery = world.repos.diffs.delivery(OWNER, alert.id)
    assert (delivery.status, delivery.attempts) == ("delivered", 1)
    assert delivery.delivered_at == world.now
    # Delivered is terminal: a duplicate job does not POST again.
    world.webhooks.deliver(OWNER, alert.id)
    assert len(post.calls) == 1


def test_bounded_retry_with_exponential_backoff_then_permanent_failure(tmp_path):
    post = FlakyPost(failures=99)
    world = changed_world(tmp_path, post)
    [alert] = world.alerts()
    gaps = []
    for _ in range(MAX_ATTEMPTS):
        fire(world, alert.id)  # attempts at the job's time
        job = world.scheduler.jobs.get(webhook_job_id(OWNER, alert.id))
        if job:
            gaps.append(job["run_date"] - world.now)
    assert gaps == [BASE_DELAY * 4 ** n for n in range(MAX_ATTEMPTS - 1)]
    assert gaps[:2] == [timedelta(seconds=30), timedelta(minutes=2)]
    assert len(post.calls) == MAX_ATTEMPTS
    delivery = world.repos.diffs.delivery(OWNER, alert.id)
    assert (delivery.status, delivery.attempts) == ("failed", MAX_ATTEMPTS)
    assert "ConnectError" in delivery.last_error
    assert webhook_job_id(OWNER, alert.id) not in world.scheduler.jobs
    # Webhook failure never touches the committed Diff, the Alert or the Run.
    assert world.diff("r2").status == "succeeded"
    assert world.repos.diffs.alert(OWNER, alert.id) == alert
    assert world.repos.runs.get(OWNER, "r2").state == RunState.COMPLETE


def test_a_retry_succeeds(tmp_path):
    post = FlakyPost(failures=2)
    world = changed_world(tmp_path, post)
    [alert] = world.alerts()
    for _ in range(3):
        fire(world, alert.id)
    assert world.repos.diffs.delivery(OWNER, alert.id).status == "delivered"
    assert len(post.calls) == 3


def test_no_webhook_configured_means_no_delivery(tmp_path):
    world = changed_world(tmp_path, FlakyPost(), url=None)
    [alert] = world.alerts()
    assert world.repos.diffs.delivery(OWNER, alert.id) is None
    assert not any(k.startswith("alert-webhook") for k in world.scheduler.jobs)


def test_delivery_errors_never_raise(tmp_path):
    def broken_store(*args, **kwargs):
        raise RuntimeError("database is locked")
    world = changed_world(tmp_path, FlakyPost())
    [alert] = world.alerts()
    world.repos.diffs.begin_delivery = broken_store
    world.webhooks.deliver(OWNER, alert.id)  # logged and rescheduled, not raised
    assert world.scheduler.jobs[webhook_job_id(OWNER, alert.id)]["run_date"] == \
        world.now + BASE_DELAY


def test_restart_reschedules_pending_deliveries_and_delivery_is_at_least_once(tmp_path):
    post = FlakyPost()
    world = changed_world(tmp_path, post)
    [alert] = world.alerts()
    # A POST succeeded but the process died before recording it.
    assert world.repos.diffs.begin_delivery(OWNER, alert.id, MAX_ATTEMPTS, NOW) == 1
    post(URL, slack_payload(alert))
    world.restart()
    world.now = NOW + timedelta(hours=1)
    world.webhooks.recover()
    job = world.scheduler.jobs[webhook_job_id(OWNER, alert.id)]
    assert job["run_date"] == world.now  # overdue: as soon as the scheduler resumes
    fire(world, alert.id)
    assert len(post.calls) == 2  # at least once, never exactly-once
    assert world.repos.diffs.delivery(OWNER, alert.id).status == "delivered"


def test_crash_loop_cannot_post_forever(tmp_path):
    world = changed_world(tmp_path, FlakyPost())
    [alert] = world.alerts()
    for _ in range(MAX_ATTEMPTS):
        assert world.repos.diffs.begin_delivery(OWNER, alert.id, MAX_ATTEMPTS, NOW)
    world.webhooks.deliver(OWNER, alert.id)
    assert world.repos.diffs.delivery(OWNER, alert.id).status == "failed"


def test_payload_notes_intervening_failed_diffs(tmp_path, monkeypatch):
    from insightforge_agent.scheduler import diff as diff_module
    real = diff_module.report_seeds

    def broken(deps, owner_id, report):
        if report.run_id == "r2":
            raise ConnectionError("down")
        return real(deps, owner_id, report)
    monkeypatch.setattr(diff_module, "report_seeds", broken)
    world = World(tmp_path / "net.db", webhook_url=URL)
    world.add_item()
    world.complete("r1", 1, fact())
    world.seed()
    world.complete("r2", 2, fact("$110", period="2030-02"))
    world.complete("r3", 3, fact("$120", period="2030-03"))
    world.process()
    world.diffs.run_item(OWNER, ITEM)
    [alert] = world.alerts()
    text = slack_payload(alert)["text"]
    assert "Diffs failed for Runs r2" in text and "net change" in text


def test_default_post_raises_on_http_error(monkeypatch):
    def fake_post(url, json, timeout):
        return httpx.Response(500, request=httpx.Request("POST", url))
    monkeypatch.setattr(webhook_module.httpx, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        post_json(URL, {"text": "x"})
