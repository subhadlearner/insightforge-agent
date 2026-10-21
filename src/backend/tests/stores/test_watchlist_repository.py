from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3
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


def test_tick_tolerates_early_jitter_and_only_misses_after_a_full_cadence(repos):
    repo = repos.watchlists
    repo.add(item())
    assert repo.take_tick("alice", "w", 2, NOW) is None
    assert repo.take_tick("alice", "w", 1, NOW - timedelta(seconds=2)) is None
    assert repo.get("alice", "w").next_tick_at == NOW
    assert repo.take_tick("alice", "w", 1, NOW - timedelta(milliseconds=500)) is not None
    assert repo.take_tick("alice", "w", 1, NOW) is None
    # A callback exactly one cadence late is accepted; only more than a cadence is missed.
    assert repo.take_tick("alice", "w", 1, NOW + timedelta(minutes=30)) is not None
    assert repo.get("alice", "w").next_tick_at == NOW + timedelta(minutes=45)
    assert repo.take_tick("alice", "w", 1, NOW + timedelta(minutes=61)) is None
    assert repo.get("alice", "w").next_tick_at == NOW + timedelta(minutes=75)
    assert repo.take_tick("alice", "w", 1, NOW + timedelta(minutes=75)) is not None


def test_edits_reject_stale_admission_and_immutable_brief_after_start(repos):
    repo = repos.watchlists
    original = item()
    repo.add(original)
    updated = original.model_copy(update={"revision": 2, "max_runs": 1})
    repo.update(updated, 1, NOW)
    with pytest.raises(WatchlistConflict):
        repo.start(run("a"), 1)
    repo.start(run("a"), 2)
    with pytest.raises(WatchlistConflict):
        repo.update(
            updated.model_copy(update={"brief": "changed", "revision": 3}),
            2,
            NOW,
        )
    with pytest.raises(WatchlistConflict):
        repo.update(updated, 1, NOW)


def test_patch_merges_current_due_time_and_reenables_a_stale_schedule(repos):
    repo = repos.watchlists
    repo.add(item(max_runs=1))
    stale = repo.get("alice", "w")
    assert repo.take_tick("alice", "w", stale.revision, NOW) is not None
    updated = stale.model_copy(update={"revision": 2, "max_runs": 2})
    merged = repo.update(updated, 1, NOW)
    assert merged.next_tick_at == NOW + timedelta(minutes=15)

    overdue = merged.model_copy(update={"revision": 3, "max_runs": 3})
    later = NOW + timedelta(hours=1)
    merged = repo.update(overdue, 2, later)
    assert merged.next_tick_at == later + timedelta(minutes=15)


def test_shutdown_guard_is_checked_inside_run_admission(repos):
    repo = repos.watchlists
    repo.add(item())
    with pytest.raises(WatchlistConflict, match="shutting down"):
        repo.start(run("a"), 1, lambda: False)
    assert repos.runs.list_for_owner("alice") == []


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
    b.watchlists.update(snapshot.model_copy(update={"revision": 2}), 1, NOW)
    with pytest.raises(WatchlistConflict):
        a.watchlists.start(run("edited"), snapshot.revision)
    snapshot = a.watchlists.get("alice", "w")
    b.watchlists.delete("alice", "w")
    with pytest.raises(NotFoundError):
        a.watchlists.start(run("deleted"), snapshot.revision)
    assert a.runs.list_for_owner("alice") == []
    a.close()
    b.close()


def test_existing_quarantine_table_gets_schema_version_migration(tmp_path):
    path = str(tmp_path / "old-watchlist.db")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE watchlist_baseline_failures ("
        "owner_id TEXT NOT NULL, item_id TEXT NOT NULL, run_id TEXT NOT NULL, "
        "reason TEXT NOT NULL, failed_at TEXT NOT NULL, "
        "PRIMARY KEY(owner_id,item_id,run_id))"
    )
    db.close()

    repos = SqliteRepos(path)
    try:
        columns = {
            row[1]
            for row in repos._db.execute(
                "PRAGMA table_info(watchlist_baseline_failures)"
            )
        }
        assert "schema_version" in columns
    finally:
        repos.close()


def test_patch_without_cadence_change_keeps_a_tick_due_within_one_cadence(repos):
    repo = repos.watchlists
    repo.add(item())
    edited = repo.get("alice", "w").model_copy(update={"revision": 2, "max_runs": 9})
    merged = repo.update(edited, 1, NOW + timedelta(minutes=1))
    assert merged.next_tick_at == NOW
    assert repo.take_tick("alice", "w", 1, NOW + timedelta(minutes=1)) is None
    assert repo.take_tick("alice", "w", 2, NOW + timedelta(minutes=1)) is not None

    recadenced = merged.model_copy(update={"revision": 3, "cadence_minutes": 30})
    later = NOW + timedelta(minutes=16)
    assert repo.update(recadenced, 2, later).next_tick_at == later + timedelta(
        minutes=30
    )


def test_recent_failed_runs_are_this_items_newest_failures_only(repos):
    repo = repos.watchlists
    repo.add(item())
    repo.add(item().model_copy(update={"id": "other"}))
    for n in range(7):
        repo.start(
            run(f"f{n}", RunState.FAILED).model_copy(
                update={"created_at": NOW + timedelta(minutes=n)}
            ),
            1,
        )
    repo.start(
        run("ok", RunState.COMPLETE).model_copy(
            update={"created_at": NOW + timedelta(hours=1)}
        ),
        1,
    )
    repo.start(
        run("elsewhere", RunState.FAILED).model_copy(
            update={
                "watchlist_item_id": "other",
                "created_at": NOW + timedelta(hours=2),
            }
        ),
        1,
    )
    assert [r.id for r in repo.recent_failed_runs("alice", "w", 5)] == [
        "f6",
        "f5",
        "f4",
        "f3",
        "f2",
    ]
    with pytest.raises(NotFoundError):
        repo.recent_failed_runs("bob", "w")
