"""SQLite Watchlist operations. BEGIN IMMEDIATE protects admission across connections too.

Run rows are the durable counter; no decrement on failure and no disposable in-memory lease.
Deleting an item preserves Runs and Reports. Already admitted work may finish after deletion.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import Run, RunState
from insightforge_agent.domain.watchlist import (
    Baseline,
    WatchlistConflict,
    WatchlistItem,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist_items (
 owner_id TEXT NOT NULL, id TEXT NOT NULL, item TEXT NOT NULL,
 PRIMARY KEY(owner_id,id)
);
CREATE TABLE IF NOT EXISTS watchlist_baselines (
 owner_id TEXT NOT NULL, item_id TEXT NOT NULL, baseline TEXT NOT NULL,
 PRIMARY KEY(owner_id,item_id)
);
CREATE INDEX IF NOT EXISTS watchlist_runs ON runs(owner_id,watchlist_item_id,state);
"""


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

    def update(self, item: WatchlistItem, expected_revision: int):
        with self._write():
            old = self.get(item.owner_id, item.id)
            if old.revision != expected_revision:
                raise WatchlistConflict(
                    "the Watchlist Item changed; reload before editing"
                )
            if old.brief != item.brief and self.counts(item.owner_id, item.id)[0]:
                raise WatchlistConflict(
                    "create a new Watchlist Item to change a Brief after Runs started"
                )
            self._save(item)

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

    def counts(self, owner_id, item_id) -> tuple[int, int, int]:
        with self._lock:
            self.get(owner_id, item_id)
            row = self._db.execute(
                "SELECT count(*), coalesce(sum(state='COMPLETE'),0), coalesce(sum(state='FAILED'),0) "
                "FROM runs WHERE owner_id=? AND watchlist_item_id=?",
                (owner_id, item_id),
            ).fetchone()
            return tuple(row)

    def start(self, run: Run, expected_revision: int):
        """Admission and counting are one commit, after executor capacity was reserved."""
        with self._write():
            item = self.get(run.owner_id, run.watchlist_item_id)
            if item.revision != expected_revision:
                raise WatchlistConflict("the Watchlist Item changed before admission")
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
            self._db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (
                    run.owner_id,
                    run.id,
                    run.brief,
                    run.state.value,
                    run.created_at.isoformat(),
                    run.watchlist_item_id,
                    None,
                ),
            )

    def take_tick(
        self, owner_id, item_id, revision, now: datetime
    ) -> WatchlistItem | None:
        """Consume a due tick exactly once, even if it overlaps or capacity later refuses it.
        A callback delayed by more than one second is a missed tick, never catch-up work."""
        with self._write():
            item = self.get(owner_id, item_id)
            if item.revision != revision or now < item.next_tick_at:
                return None
            due = item.after(now) - timedelta(minutes=item.cadence_minutes)
            late = (now - due).total_seconds() > 1
            self._save(item.model_copy(update={"next_tick_at": item.after(now)}))
            return None if late else item

    def skip_missed(self, owner_id, item_id, now: datetime) -> WatchlistItem:
        with self._write():
            item = self.get(owner_id, item_id)
            item = item.model_copy(update={"next_tick_at": item.after(now)})
            self._save(item)
            return item

    def baseline(self, owner_id, item_id) -> Baseline | None:
        with self._lock:
            self.get(owner_id, item_id)
            row = self._db.execute(
                "SELECT baseline FROM watchlist_baselines WHERE owner_id=? AND item_id=?",
                (owner_id, item_id),
            ).fetchone()
            return Baseline.model_validate_json(row[0]) if row else None

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

    def __init__(self, runs, lock):
        self._runs, self._lock = runs, lock
        self._items = {}
        self._baselines = {}

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

    def update(self, item, expected_revision):
        with self._lock:
            old = self.get(item.owner_id, item.id)
            if old.revision != expected_revision:
                raise WatchlistConflict(
                    "the Watchlist Item changed; reload before editing"
                )
            if old.brief != item.brief and self.counts(item.owner_id, item.id)[0]:
                raise WatchlistConflict(
                    "create a new Watchlist Item to change a Brief after Runs started"
                )
            self._items[item.owner_id, item.id] = item.model_copy(deep=True)

    def delete(self, owner_id, item_id):
        with self._lock:
            self.get(owner_id, item_id)
            del self._items[owner_id, item_id]
            self._baselines.pop((owner_id, item_id), None)

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

    def start(self, run, expected_revision):
        with self._lock:
            item = self.get(run.owner_id, run.watchlist_item_id)
            if item.revision != expected_revision:
                raise WatchlistConflict("the Watchlist Item changed before admission")
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
            if item.revision != revision or now < item.next_tick_at:
                return None
            self._items[owner_id, item_id] = item.model_copy(
                update={"next_tick_at": item.after(now)}
            )
            due = item.after(now) - timedelta(minutes=item.cadence_minutes)
            return None if (now - due).total_seconds() > 1 else item

    def skip_missed(self, owner_id, item_id, now):
        with self._lock:
            item = self.get(owner_id, item_id)
            item = item.model_copy(update={"next_tick_at": item.after(now)})
            self._items[owner_id, item_id] = item
            return item

    def baseline(self, owner_id, item_id):
        with self._lock:
            self.get(owner_id, item_id)
            baseline = self._baselines.get((owner_id, item_id))
            return baseline.model_copy(deep=True) if baseline else None

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
