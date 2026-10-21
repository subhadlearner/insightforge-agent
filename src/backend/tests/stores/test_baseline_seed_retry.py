"""T20 B4 (T12-2): a Baseline seed whose normalisation keeps failing is retried with a
persisted backoff (1, 2, 4, 8, 16, 32 minutes, then every 60 minutes, indefinitely), enforced
where the seed is processed, so neither a duplicate job, a Run's completion nor a restart can
call the Finding judge early. Success establishes the original seed and clears the backoff;
there is no permanent-failure state. SQLite files, a recording scheduler, a scripted judge."""

from datetime import timedelta

import pytest

from langchain_core.messages import AIMessage

from insightforge_agent.agents.finding_judge import FindingJudge
from insightforge_agent.domain.findings import DEFAULT_REGISTRY
from insightforge_agent.scheduler.diff import SEED_RETRY_DELAYS, seed_retry_delay
from insightforge_agent.scheduler.watchlist import diff_job_id
from tests.scripted import ScriptedChat
from tests.stores.diff_world import ITEM, NOW, OWNER, World, fact, unsupported_reply

MINUTE = timedelta(minutes=1)


class CountingJudgeModel:
    """Fails with `bad` (malformed JSON, or no judgment) while `failing`, then answers;
    counts every call."""

    def __init__(self, bad="{not json"):
        self.calls = 0
        self.failing = True
        self.during_call = lambda: None

        def reply(messages):
            self.calls += 1
            self.during_call()
            return AIMessage(content=bad) if self.failing else unsupported_reply(messages)
        self.model = ScriptedChat(reply=reply)


def failing_world(tmp_path, bad="{not json"):
    judge = CountingJudgeModel(bad)
    w = World(tmp_path / "seed.db", judge=FindingJudge(judge.model, DEFAULT_REGISTRY))
    w.add_item()
    w.complete("r1", 1, fact("Model X", predicate="offers"))
    assert w.seed() is not None
    return w, judge


def job_at(w):
    return w.scheduler.jobs[diff_job_id(OWNER, ITEM)]["run_date"]


def test_the_schedule_is_1_2_4_8_16_32_then_60_minutes_indefinitely():
    assert [seed_retry_delay(n) / MINUTE for n in range(1, 11)] == \
        [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert SEED_RETRY_DELAYS[-1] == timedelta(minutes=60)


@pytest.mark.parametrize("bad", ["{not json", '{"judgments": []}'])
def test_each_failure_waits_longer_and_early_callbacks_never_call_the_judge(tmp_path, bad):
    w, judge = failing_world(tmp_path, bad)
    expected_waits = [1, 2, 4, 8, 16, 32, 60, 60]
    for attempt, wait in enumerate(expected_waits, start=1):
        w.diffs.run_item(OWNER, ITEM)
        assert judge.calls == attempt
        retry_at = w.now + wait * MINUTE
        assert w.repos.diffs.seed_retry_after(OWNER, ITEM) == retry_at
        assert job_at(w) == retry_at
        assert w.repos.watchlists.baseline_state(OWNER, ITEM) == "seed"
        # Duplicate jobs, an immediate job from a completion, just before the time: no call.
        w.now = retry_at - timedelta(seconds=1)
        w.diffs.run_item(OWNER, ITEM)
        w.diffs.process(OWNER, ITEM)
        w.schedules.completed(w.repos.runs.get(OWNER, "r1"))
        w.diffs.run_item(OWNER, ITEM)
        assert judge.calls == attempt
        assert job_at(w) == retry_at
        w.now = retry_at
    w.repos.close()


def test_the_backoff_runs_from_the_failure_not_from_the_start_of_a_slow_judge_call(tmp_path):
    w, judge = failing_world(tmp_path)
    started = w.now

    def slow_call():
        w.now += 3 * MINUTE  # the failing call takes three minutes

    judge.during_call = slow_call
    w.diffs.run_item(OWNER, ITEM)
    failed_at = started + 3 * MINUTE
    assert judge.calls == 1 and w.now == failed_at
    retry_at = w.repos.diffs.seed_retry_after(OWNER, ITEM)
    assert retry_at == failed_at + MINUTE and job_at(w) == retry_at
    judge.during_call = lambda: None
    w.now = retry_at - timedelta(seconds=1)  # already past `started + 1 minute`
    w.diffs.run_item(OWNER, ITEM)
    assert judge.calls == 1
    w.repos.close()


def test_the_backoff_survives_a_restart_and_recovery_cannot_bring_it_forward(tmp_path):
    w, judge = failing_world(tmp_path)
    for _ in range(3):  # waits 1, 2, then 4 minutes
        w.diffs.run_item(OWNER, ITEM)
        w.now = w.repos.diffs.seed_retry_after(OWNER, ITEM)
    w.now -= 4 * MINUTE  # back to the third failure's time
    retry_at = w.repos.diffs.seed_retry_after(OWNER, ITEM)
    assert retry_at == w.now + 4 * MINUTE and judge.calls == 3
    w.restart()
    w.schedules.recover()  # schedules an immediate Diff job for the item
    w.diffs.run_item(OWNER, ITEM)
    assert judge.calls == 3 and job_at(w) == retry_at
    w.now = retry_at
    w.diffs.run_item(OWNER, ITEM)
    assert judge.calls == 4
    w.repos.close()


def test_recovery_establishes_the_original_seed_clears_the_backoff_and_diffs_in_order(
        tmp_path):
    w, judge = failing_world(tmp_path)
    w.diffs.run_item(OWNER, ITEM)
    w.now += MINUTE
    w.diffs.run_item(OWNER, ITEM)
    # Later Runs complete meanwhile; none is diffed while the seed is not established.
    w.complete("r2", 2, fact("Model X", predicate="offers", period="2030-02"))
    w.complete("r3", 3, fact("Model X", predicate="offers", period="2030-03"))
    w.schedules.completed(w.repos.runs.get(OWNER, "r3"))
    w.diffs.run_item(OWNER, ITEM)
    assert w.diff("r2") is None and w.diff("r3") is None and judge.calls == 2
    judge.failing = False
    w.now = w.repos.diffs.seed_retry_after(OWNER, ITEM)
    w.diffs.run_item(OWNER, ITEM)
    assert w.repos.watchlists.baseline_state(OWNER, ITEM) == "established"
    assert w.repos.diffs.findings(OWNER, ITEM)[1]  # the seed's Finding
    # The original first-success seed was established, and r2 reconfirmed its Finding.
    assert w.repos.watchlists.baseline(OWNER, ITEM).initializing_run_id == "r1"
    assert [e.outcome for e in w.diff("r2").entries if e.finding] == ["reconfirmed"]
    assert w.repos.diffs.seed_retry_after(OWNER, ITEM) is None
    assert [d.run_id for d in w.repos.diffs.diffs(OWNER, ITEM)] == ["r2", "r3"]
    assert all(d.status == "succeeded" for d in w.repos.diffs.diffs(OWNER, ITEM))
    calls = judge.calls
    w.diffs.run_item(OWNER, ITEM)  # nothing left: no further judge call, no new record
    assert judge.calls == calls and len(w.repos.diffs.diffs(OWNER, ITEM)) == 2
    assert w.alerts() == []
    w.repos.close()


def test_deleting_the_item_removes_its_backoff(tmp_path):
    w, _ = failing_world(tmp_path)
    w.diffs.run_item(OWNER, ITEM)
    assert w.repos.diffs.seed_retry_after(OWNER, ITEM) is not None
    w.repos.watchlists.delete(OWNER, ITEM)
    assert w.repos.diffs.seed_retry_after(OWNER, ITEM) is None
    w.repos.close()


def test_a_backoff_belongs_to_its_owner_and_item(tmp_path):
    w, _ = failing_world(tmp_path)
    w.diffs.run_item(OWNER, ITEM)
    assert w.repos.diffs.seed_retry_after("someone-else", ITEM) is None
    assert w.repos.diffs.seed_retry_after(OWNER, "other-item") is None
    assert NOW < w.repos.diffs.seed_retry_after(OWNER, ITEM)
    w.repos.close()
