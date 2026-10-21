"""SQLite Watchlist operations. BEGIN IMMEDIATE protects admission across connections too.

Run rows are the durable counter; no decrement on failure and no disposable in-memory lease.
Deleting an item preserves Runs and Reports. Already admitted work may finish after deletion.
"""

import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import Report, Run, RunState
from insightforge_agent.domain.watchlist import (
    Baseline,
    WatchlistConflict,
    WatchlistItem,
    check_watchlist_brief_change,
)
from insightforge_agent.stores.run_rows import insert_run_row, run_from_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist_items (
 owner_id TEXT NOT NULL, id TEXT NOT NULL, item TEXT NOT NULL,
 PRIMARY KEY(owner_id,id)
);
CREATE TABLE IF NOT EXISTS watchlist_baselines (
 owner_id TEXT NOT NULL, item_id TEXT NOT NULL, baseline TEXT NOT NULL,
 PRIMARY KEY(owner_id,item_id)
);
CREATE TABLE IF NOT EXISTS watchlist_baseline_failures (
 owner_id TEXT NOT NULL, item_id TEXT NOT NULL, run_id TEXT NOT NULL,
 reason TEXT NOT NULL, failed_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(owner_id,item_id,run_id)
);
CREATE INDEX IF NOT EXISTS watchlist_runs ON runs(owner_id,watchlist_item_id,state);
"""


def merged_next_tick_at(old: WatchlistItem, item: WatchlistItem, now: datetime):
    """The due time an edit keeps. A new cadence restarts one cadence from now, and so does a
    schedule more than one cadence overdue (an exhausted item being re-enabled). A tick due
    within the last cadence stays due, so the edit's new revision still runs it."""
    step = timedelta(minutes=item.cadence_minutes)
    if item.cadence_minutes != old.cadence_minutes or now - old.next_tick_at > step:
        return now + step
    return old.next_tick_at


class SqliteWatchlistRepository:
    def __init__(self, db, lock):
        self._db, self._lock = db, lock

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                yield

    def get(self, owner_id, item_id) -> WatchlistItem:
        with self._lock:
            row = self._db.execute(
                "SELECT item FROM watchlist_items WHERE owner_id=? AND id=?",
                (owner_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"watchlist item {item_id}")
        return WatchlistItem.model_validate_json(row[0])

    def list_for_owner(self, owner_id) -> list[WatchlistItem]:
        with self._lock:
            rows = self._db.execute(
                "SELECT item FROM watchlist_items WHERE owner_id=? ORDER BY id",
                (owner_id,),
            ).fetchall()
        return [WatchlistItem.model_validate_json(r[0]) for r in rows]

    def list_all(self) -> list[WatchlistItem]:
        """System startup only; API reads use list_for_owner."""
        with self._lock:
            rows = self._db.execute(
                "SELECT item FROM watchlist_items ORDER BY owner_id,id"
            ).fetchall()
        return [WatchlistItem.model_validate_json(r[0]) for r in rows]

    def add(self, item: WatchlistItem):
        with self._write():
            self._db.execute(
                "INSERT INTO watchlist_items VALUES (?,?,?)",
                (item.owner_id, item.id, item.model_dump_json()),
            )

    def _save(self, item):
        self._db.execute(
            "UPDATE watchlist_items SET item=? WHERE owner_id=? AND id=?",
            (item.model_dump_json(), item.owner_id, item.id),
        )

    def update(
        self,
        item: WatchlistItem,
        expected_revision: int,
        now: datetime | Callable[[], datetime],
    ) -> WatchlistItem:
        with self._write():
            now = now() if callable(now) else now
            old = self.get(item.owner_id, item.id)
            if old.revision != expected_revision:
                raise WatchlistConflict(
                    "the Watchlist Item changed; reload before editing"
                )
            check_watchlist_brief_change(
                old, item, self.counts(item.owner_id, item.id)[0]
            )
            item = item.model_copy(
                update={"next_tick_at": merged_next_tick_at(old, item, now)}
            )
            self._save(item)
            return item

    def delete(self, owner_id, item_id):
        with self._write():
            self.get(owner_id, item_id)
            self._db.execute(
                "DELETE FROM watchlist_items WHERE owner_id=? AND id=?",
                (owner_id, item_id),
            )
            self._db.execute(
                "DELETE FROM watchlist_baselines WHERE owner_id=? AND item_id=?",
                (owner_id, item_id),
            )
            self._db.execute(
                "DELETE FROM watchlist_baseline_failures WHERE owner_id=? AND item_id=?",
                (owner_id, item_id),
            )
            # T12 established state goes with the seed, and so does its normalisation backoff
            # (T20 B4); the Diff log and Alerts are kept.
            for table in ("watchlist_findings", "watchlist_baseline_state",
                          "watchlist_seed_retries"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE owner_id=? AND item_id=?",
                    (owner_id, item_id),
                )

    def counts(self, owner_id, item_id) -> tuple[int, int, int]:
        with self._lock:
            self.get(owner_id, item_id)
            row = self._db.execute(
                "SELECT count(*), coalesce(sum(state='COMPLETE'),0), coalesce(sum(state='FAILED'),0) "
                "FROM runs WHERE owner_id=? AND watchlist_item_id=?",
                (owner_id, item_id),
            ).fetchone()
            return tuple(row)

    def recent_failed_runs(self, owner_id, item_id, limit=5) -> list[Run]:
        """Newest first, through the Watchlist Run index; never every Run of the owner."""
        with self._lock:
            self.get(owner_id, item_id)
            rows = self._db.execute(
                "SELECT * FROM runs WHERE owner_id=? AND watchlist_item_id=? "
                "AND state='FAILED' ORDER BY created_at DESC, id DESC LIMIT ?",
                (owner_id, item_id, limit),
            ).fetchall()
        return [run_from_row(r) for r in rows]

    def start(
        self,
        run: Run,
        expected_revision: int,
        admission_open: Callable[[], bool] | None = None,
    ):
        """Admission and counting are one commit, after executor capacity was reserved."""
        with self._write():
            item = self.get(run.owner_id, run.watchlist_item_id)
            if item.revision != expected_revision:
                raise WatchlistConflict("the Watchlist Item changed before admission")
            if admission_open is not None and not admission_open():
                raise WatchlistConflict("the process is shutting down")
            started = self.counts(item.owner_id, item.id)[0]
            if item.max_runs is not None and started >= item.max_runs:
                raise WatchlistConflict("max_runs reached")
            active = self._db.execute(
                "SELECT 1 FROM runs WHERE owner_id=? AND watchlist_item_id=? "
                "AND state NOT IN ('COMPLETE','FAILED') LIMIT 1",
                (item.owner_id, item.id),
            ).fetchone()
            if active:
                raise WatchlistConflict("a Run is already active")
            insert_run_row(self._db, run)

    def take_tick(
        self, owner_id, item_id, revision, now: datetime
    ) -> WatchlistItem | None:
        """Consume one due callback and advance past older intervals atomically.

        Dispatch delay through one cadence is ordinary jitter. More than one cadence late is
        a missed callback: advance the durable clock but admit no Run. An APScheduler fire up
        to one second early is accepted; an earlier callback leaves the due time untouched.
        """
        with self._write():
            item = self.get(owner_id, item_id)
            if item.revision != revision:
                return None
            if now < item.next_tick_at - timedelta(seconds=1):
                return None
            effective_now = max(now, item.next_tick_at)
            missed = (effective_now - item.next_tick_at).total_seconds() > (
                item.cadence_minutes * 60
            )
            self._save(
                item.model_copy(update={"next_tick_at": item.after(effective_now)})
            )
            return None if missed else item

    def skip_missed(self, owner_id, item_id, now: datetime) -> WatchlistItem:
        with self._write():
            item = self.get(owner_id, item_id)
            item = item.model_copy(update={"next_tick_at": item.after(now)})
            self._save(item)
            return item

    def baseline_candidates(self, owner_id, item_id, schema_version) -> list[Report]:
        """Use the Watchlist Run index; never scan every Report belonging to the owner."""
        with self._lock:
            self.get(owner_id, item_id)
            rows = self._db.execute(
                "SELECT p.owner_id,p.id,p.run_id,p.created_at,p.body "
                "FROM runs r JOIN reports p ON p.owner_id=r.owner_id AND p.run_id=r.id "
                "LEFT JOIN watchlist_baseline_failures f "
                "ON f.owner_id=r.owner_id AND f.item_id=r.watchlist_item_id "
                "AND f.run_id=r.id AND f.schema_version=? "
                "WHERE r.owner_id=? AND r.watchlist_item_id=? AND r.state='COMPLETE' "
                "AND f.run_id IS NULL ORDER BY p.created_at,p.id",
                (schema_version, owner_id, item_id),
            ).fetchall()
        return [
            Report(
                owner_id=row[0],
                id=row[1],
                run_id=row[2],
                created_at=datetime.fromisoformat(row[3]),
                body=json.loads(row[4]),
            )
            for row in rows
        ]

    def reject_baseline_candidate(
        self, owner_id, item_id, run_id, reason, failed_at, schema_version
    ) -> bool:
        with self._write():
            self.get(owner_id, item_id)
            run = self._db.execute(
                "SELECT state,watchlist_item_id FROM runs WHERE owner_id=? AND id=?",
                (owner_id, run_id),
            ).fetchone()
            if run != (RunState.COMPLETE.value, item_id):
                raise ValueError("only a COMPLETE Run of this Watchlist Item can be rejected")
            current = self._db.execute(
                "SELECT schema_version FROM watchlist_baseline_failures "
                "WHERE owner_id=? AND item_id=? AND run_id=?",
                (owner_id, item_id, run_id),
            ).fetchone()
            self._db.execute(
                "INSERT INTO watchlist_baseline_failures "
                "(owner_id,item_id,run_id,reason,failed_at,schema_version) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(owner_id,item_id,run_id) DO UPDATE SET "
                "reason=excluded.reason, failed_at=excluded.failed_at, "
                "schema_version=excluded.schema_version",
                (
                    owner_id,
                    item_id,
                    run_id,
                    reason[:1000],
                    failed_at.isoformat(),
                    schema_version,
                ),
            )
            return current is None or current[0] != schema_version

    def baseline(self, owner_id, item_id) -> Baseline | None:
        with self._lock:
            self.get(owner_id, item_id)
            row = self._db.execute(
                "SELECT baseline FROM watchlist_baselines WHERE owner_id=? AND item_id=?",
                (owner_id, item_id),
            ).fetchone()
            return Baseline.model_validate_json(row[0]) if row else None

    def baseline_state(self, owner_id, item_id) -> str:
        with self._lock:
            self.get(owner_id, item_id)
            established = self._db.execute(
                "SELECT 1 FROM watchlist_baseline_state WHERE owner_id=? AND item_id=?",
                (owner_id, item_id),
            ).fetchone()
            if established:
                return "established"
            return "seed" if self.baseline(owner_id, item_id) else "none"

    def initialize_baseline(self, baseline: Baseline) -> Baseline:
        with self._write():
            self.get(baseline.owner_id, baseline.watchlist_item_id)
            run = self._db.execute(
                "SELECT state,watchlist_item_id FROM runs WHERE owner_id=? AND id=?",
                (baseline.owner_id, baseline.initializing_run_id),
            ).fetchone()
            if run != (RunState.COMPLETE.value, baseline.watchlist_item_id):
                raise ValueError(
                    "only a COMPLETE Run of this Watchlist Item can initialize it"
                )
            self._db.execute(
                "INSERT OR IGNORE INTO watchlist_baselines VALUES (?,?,?)",
                (
                    baseline.owner_id,
                    baseline.watchlist_item_id,
                    baseline.model_dump_json(),
                ),
            )
            return self.baseline(baseline.owner_id, baseline.watchlist_item_id)


class MemoryWatchlistRepository:
    """Offline fake with the same first-winner contracts as SQLite."""

    def __init__(self, runs, reports, lock):
        self._runs, self._reports, self._lock = runs, reports, lock
        self._items = {}
        self._baselines = {}
        self._baseline_failures = set()

    def get(self, owner_id, item_id):
        with self._lock:
            try:
                return self._items[owner_id, item_id].model_copy(deep=True)
            except KeyError:
                raise NotFoundError(f"watchlist item {item_id}") from None

    def list_all(self):
        with self._lock:
            return [self.get(*k) for k in sorted(self._items)]

    def list_for_owner(self, owner_id):
        return [i for i in self.list_all() if i.owner_id == owner_id]

    def add(self, item):
        with self._lock:
            key = (item.owner_id, item.id)
            if key in self._items:
                raise ValueError("Watchlist Item already exists")
            self._items[key] = item.model_copy(deep=True)

    def update(self, item, expected_revision, now):
        with self._lock:
            now = now() if callable(now) else now
            old = self.get(item.owner_id, item.id)
            if old.revision != expected_revision:
                raise WatchlistConflict(
                    "the Watchlist Item changed; reload before editing"
                )
            check_watchlist_brief_change(
                old, item, self.counts(item.owner_id, item.id)[0]
            )
            item = item.model_copy(
                update={"next_tick_at": merged_next_tick_at(old, item, now)}
            )
            self._items[item.owner_id, item.id] = item.model_copy(deep=True)
            return item

    def delete(self, owner_id, item_id):
        with self._lock:
            self.get(owner_id, item_id)
            del self._items[owner_id, item_id]
            self._baselines.pop((owner_id, item_id), None)
            self._baseline_failures = {
                key for key in self._baseline_failures if key[:2] != (owner_id, item_id)
            }

    def counts(self, owner_id, item_id):
        with self._lock:
            self.get(owner_id, item_id)
            runs = [
                r
                for r in self._runs.list_for_owner(owner_id)
                if r.watchlist_item_id == item_id
            ]
            return (
                len(runs),
                sum(r.state == RunState.COMPLETE for r in runs),
                sum(r.state == RunState.FAILED for r in runs),
            )

    def recent_failed_runs(self, owner_id, item_id, limit=5):
        with self._lock:
            self.get(owner_id, item_id)
            failed = [
                r
                for r in self._runs.list_for_owner(owner_id)
                if r.watchlist_item_id == item_id and r.state == RunState.FAILED
            ]
            return list(reversed(failed))[:limit]

    def start(self, run, expected_revision, admission_open=None):
        with self._lock:
            item = self.get(run.owner_id, run.watchlist_item_id)
            if item.revision != expected_revision:
                raise WatchlistConflict("the Watchlist Item changed before admission")
            if admission_open is not None and not admission_open():
                raise WatchlistConflict("the process is shutting down")
            if (
                item.max_runs is not None
                and self.counts(item.owner_id, item.id)[0] >= item.max_runs
            ):
                raise WatchlistConflict("max_runs reached")
            if any(
                r.watchlist_item_id == item.id
                and r.state not in (RunState.COMPLETE, RunState.FAILED)
                for r in self._runs.list_for_owner(item.owner_id)
            ):
                raise WatchlistConflict("a Run is already active")
            self._runs.add(run)

    def take_tick(self, owner_id, item_id, revision, now):
        with self._lock:
            item = self.get(owner_id, item_id)
            if item.revision != revision:
                return None
            if now < item.next_tick_at - timedelta(seconds=1):
                return None
            effective_now = max(now, item.next_tick_at)
            missed = (effective_now - item.next_tick_at).total_seconds() > (
                item.cadence_minutes * 60
            )
            self._items[owner_id, item_id] = item.model_copy(
                update={"next_tick_at": item.after(effective_now)}
            )
            return None if missed else item

    def skip_missed(self, owner_id, item_id, now):
        with self._lock:
            item = self.get(owner_id, item_id)
            item = item.model_copy(update={"next_tick_at": item.after(now)})
            self._items[owner_id, item_id] = item
            return item

    def baseline_candidates(self, owner_id, item_id, schema_version):
        with self._lock:
            self.get(owner_id, item_id)
            candidates = []
            for run in self._runs.list_for_owner(owner_id):
                key = (owner_id, item_id, run.id, schema_version)
                if (
                    run.watchlist_item_id == item_id
                    and run.state == RunState.COMPLETE
                    and key not in self._baseline_failures
                ):
                    try:
                        candidates.append(self._reports.get_for_run(owner_id, run.id))
                    except NotFoundError:
                        pass
            return sorted(candidates, key=lambda report: (report.created_at, report.id))

    def reject_baseline_candidate(
        self, owner_id, item_id, run_id, reason, failed_at, schema_version
    ):
        with self._lock:
            self.get(owner_id, item_id)
            run = self._runs.get(owner_id, run_id)
            if run.state != RunState.COMPLETE or run.watchlist_item_id != item_id:
                raise ValueError("only a COMPLETE Run of this Watchlist Item can be rejected")
            key = (owner_id, item_id, run_id, schema_version)
            first = key not in self._baseline_failures
            self._baseline_failures.add(key)
            return first

    def baseline(self, owner_id, item_id):
        with self._lock:
            self.get(owner_id, item_id)
            baseline = self._baselines.get((owner_id, item_id))
            return baseline.model_copy(deep=True) if baseline else None

    def baseline_state(self, owner_id, item_id):
        """The fake has no Diff store (T12 runs on SQLite only): never established."""
        return "seed" if self.baseline(owner_id, item_id) else "none"

    def initialize_baseline(self, baseline):
        with self._lock:
            self.get(baseline.owner_id, baseline.watchlist_item_id)
            run = self._runs.get(baseline.owner_id, baseline.initializing_run_id)
            if (
                run.state != RunState.COMPLETE
                or run.watchlist_item_id != baseline.watchlist_item_id
            ):
                raise ValueError(
                    "only a COMPLETE Run of this Watchlist Item can initialize it"
                )
            self._baselines.setdefault(
                (baseline.owner_id, baseline.watchlist_item_id),
                baseline.model_copy(deep=True),
            )
            return self.baseline(baseline.owner_id, baseline.watchlist_item_id)
