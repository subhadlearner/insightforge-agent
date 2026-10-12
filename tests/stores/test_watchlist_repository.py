from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading

import pytest

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import Run, RunState
from insightforge_agent.domain.watchlist import (
    Baseline,
    WatchlistConflict,
    WatchlistItem,
)
from insightforge_agent.stores.sqlite import SqliteRepos

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def item(**kw):
    return WatchlistItem(
        id="w",
        owner_id="alice",
        brief="EV market",
        cadence_minutes=15,
        created_at=NOW,
        next_tick_at=NOW,
        **kw,
    )


def run(rid, state=RunState.PLANNING):
    return Run(
        id=rid,
        owner_id="alice",
        brief="EV market",
        state=state,
        created_at=NOW,
        watchlist_item_id="w",
    )


def test_counter_overlap_and_first_success(repos):
    repo = repos.watchlists
    repo.add(item(max_runs=2))
    repo.start(run("a"), 1)
    with pytest.raises(WatchlistConflict, match="active"):
        repo.start(run("b"), 1)
    assert repo.counts("alice", "w") == (1, 0, 0)
    baseline = Baseline(
        owner_id="alice",
        watchlist_item_id="w",
        initialized_at=NOW,
        initializing_run_id="a",
        seeds=[],
    )
    repos.runs.set_state("alice", "a", RunState.FAILED)
    with pytest.raises(ValueError):
        repo.initialize_baseline(baseline)
    assert repo.baseline("alice", "w") is None
    repo.start(run("b"), 1)
    repos.runs.set_state("alice", "b", RunState.COMPLETE)
    baseline = baseline.model_copy(update={"initializing_run_id": "b"})
    assert repo.initialize_baseline(baseline) == baseline
    assert (
        repo.initialize_baseline(
            baseline.model_copy(update={"initialized_at": NOW + timedelta(days=1)})
        )
        == baseline
    )
    assert repo.counts("alice", "w") == (2, 1, 1)
    with pytest.raises(WatchlistConflict, match="max_runs"):
        repo.start(run("c"), 1)
    with pytest.raises(NotFoundError):
        repo.baseline("bob", "w")
    repo.delete("alice", "w")
    assert len(repos.runs.list_for_owner("alice")) == 2


@pytest.mark.parametrize(
    "state", [s for s in RunState if s not in (RunState.FAILED, RunState.COMPLETE)]
)
def test_every_nonterminal_state_blocks_overlap(repos, state):
    repos.watchlists.add(item())
    repos.watchlists.start(run("a", state), 1)
    with pytest.raises(WatchlistConflict):
        repos.watchlists.start(run("b"), 1)


def test_tick_is_consumed_once_missed_ticks_skip_and_revision_rejects(repos):
    repo = repos.watchlists
    repo.add(item())
    assert repo.take_tick("alice", "w", 2, NOW) is None
    assert repo.take_tick("alice", "w", 1, NOW) is not None
    assert repo.take_tick("alice", "w", 1, NOW) is None
    assert repo.take_tick("alice", "w", 1, NOW + timedelta(minutes=16)) is None
    assert repo.get("alice", "w").next_tick_at == NOW + timedelta(minutes=30)
    # APScheduler can itself drop an earlier misfire; the current timely tick still runs.
    assert repo.take_tick("alice", "w", 1, NOW + timedelta(minutes=60)) is not None
    assert repo.get("alice", "w").next_tick_at == NOW + timedelta(minutes=75)


def test_edits_reject_stale_admission_and_immutable_brief_after_start(repos):
    repo = repos.watchlists
    original = item()
    repo.add(original)
    updated = original.model_copy(update={"revision": 2, "max_runs": 1})
    repo.update(updated, 1)
    with pytest.raises(WatchlistConflict):
        repo.start(run("a"), 1)
    repo.start(run("a"), 2)
    with pytest.raises(WatchlistConflict):
        repo.update(updated.model_copy(update={"brief": "changed", "revision": 3}), 2)
    with pytest.raises(WatchlistConflict):
        repo.update(updated, 1)


def test_two_connections_admit_only_one_and_counter_survives_restart(tmp_path):
    path = str(tmp_path / "shared.db")
    a, b = SqliteRepos(path), SqliteRepos(path)
    a.watchlists.add(item(max_runs=1))
    gate = threading.Barrier(2)

    def attempt(repos, rid):
        gate.wait()
        try:
            repos.watchlists.start(run(rid), 1)
            return True
        except WatchlistConflict:
            return False

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(attempt, a, "a"), pool.submit(attempt, b, "b")]
        assert sorted(f.result() for f in futures) == [False, True]
    a.close()
    b.close()
    c = SqliteRepos(path)
    try:
        assert c.watchlists.counts("alice", "w") == (1, 0, 0)
    finally:
        c.close()


def test_concurrent_tick_consumption_across_connections(tmp_path):
    path = str(tmp_path / "ticks.db")
    a, b = SqliteRepos(path), SqliteRepos(path)
    a.watchlists.add(item())
    gate = threading.Barrier(2)

    def attempt(repos):
        gate.wait()
        return repos.watchlists.take_tick("alice", "w", 1, NOW)

    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(attempt, r) for r in (a, b)]
            assert sum(f.result() is not None for f in futures) == 1
        assert a.watchlists.get("alice", "w").next_tick_at == NOW + timedelta(
            minutes=15
        )
    finally:
        a.close()
        b.close()


def test_checked_run_is_rejected_after_concurrent_deletion_or_edit(tmp_path):
    path = str(tmp_path / "edit.db")
    a, b = SqliteRepos(path), SqliteRepos(path)
    a.watchlists.add(item())
    snapshot = a.watchlists.get("alice", "w")
    b.watchlists.update(snapshot.model_copy(update={"revision": 2}), 1)
    with pytest.raises(WatchlistConflict):
        a.watchlists.start(run("edited"), snapshot.revision)
    snapshot = a.watchlists.get("alice", "w")
    b.watchlists.delete("alice", "w")
    with pytest.raises(NotFoundError):
        a.watchlists.start(run("deleted"), snapshot.revision)
    assert a.runs.list_for_owner("alice") == []
    a.close()
    b.close()
