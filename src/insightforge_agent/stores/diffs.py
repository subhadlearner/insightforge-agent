"""SQLite established Baseline state, Diff log, Alerts and webhook deliveries (T12).

Every decision that must survive a crash or a duplicate callback is a persistent constraint:

- `watchlist_baseline_state` exists only once a seed was normalised (seed -> established); its
  `version` is compared and incremented by every committed Diff, so two concurrent Diffs cannot
  both apply.
- `watchlist_diffs` has one row per Run; `attempts` is incremented durably before each attempt,
  so a crash loop still ends in a permanent failure, and a terminal row is never reopened. A
  failed attempt stores `retry_after`, the earliest time the retry may run, so no later
  completion callback or restart can bring the retry forward.
- A commit applies only to the earliest unfinished Diff of the item (`_next_run`), so a later Run
  never overtakes an earlier one.
- `watchlist_alerts` is UNIQUE per Run; its delivery row is written in the same transaction.

The Diff commit (Finding updates, version, Diff record, Alert, delivery) is one transaction.
"""

import json
from contextlib import contextmanager
from datetime import datetime
from typing import NamedTuple

from insightforge_agent.domain.alerts import (
    Alert,
    DiffRecord,
    FailedDiff,
    WebhookDelivery,
)
from insightforge_agent.domain.diff import DiffEntry, DiffResult, EstablishedFinding
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.findings import Skipped

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist_baseline_state (
 owner_id TEXT NOT NULL, item_id TEXT NOT NULL, initializing_run_id TEXT NOT NULL,
 version INTEGER NOT NULL, established_at TEXT NOT NULL, detail TEXT NOT NULL,
 PRIMARY KEY(owner_id,item_id)
);
CREATE TABLE IF NOT EXISTS watchlist_findings (
 owner_id TEXT NOT NULL, item_id TEXT NOT NULL, finding_key TEXT NOT NULL,
 state TEXT NOT NULL,
 PRIMARY KEY(owner_id,item_id,finding_key)
);
CREATE TABLE IF NOT EXISTS watchlist_diffs (
 owner_id TEXT NOT NULL, item_id TEXT NOT NULL, run_id TEXT NOT NULL, report_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '{}', alert_id TEXT, updated_at TEXT NOT NULL,
 retry_after TEXT,
 PRIMARY KEY(owner_id,item_id,run_id)
);
CREATE TABLE IF NOT EXISTS watchlist_alerts (
 owner_id TEXT NOT NULL, id TEXT NOT NULL, item_id TEXT NOT NULL, run_id TEXT NOT NULL,
 created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0, alert TEXT NOT NULL,
 PRIMARY KEY(owner_id,id), UNIQUE(owner_id,run_id)
);
CREATE TABLE IF NOT EXISTS alert_deliveries (
 owner_id TEXT NOT NULL, alert_id TEXT NOT NULL, url TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
 last_error TEXT NOT NULL DEFAULT '', delivered_at TEXT,
 PRIMARY KEY(owner_id,alert_id)
);
"""

_ERROR_CHARS = 1000


class NextDiff(NamedTuple):
    run_id: str
    report_id: str | None
    retry_after: datetime | None  # set after a failed attempt: not before this time


class DiffConflict(Exception):
    """The Baseline moved, or an earlier Diff is unfinished: recompute from current state."""


class SqliteDiffRepository:
    def __init__(self, db, lock):
        self._db, self._lock = db, lock

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                yield

    def _item(self, owner_id, item_id):
        if self._db.execute(
            "SELECT 1 FROM watchlist_items WHERE owner_id=? AND id=?", (owner_id, item_id)
        ).fetchone() is None:
            raise NotFoundError(f"watchlist item {item_id}")

    def _initializing_run(self, owner_id, item_id) -> str | None:
        row = self._db.execute(
            "SELECT initializing_run_id FROM watchlist_baseline_state "
            "WHERE owner_id=? AND item_id=?", (owner_id, item_id)).fetchone()
        return row[0] if row else None

    # --- seed -> established ----------------------------------------------------------------

    def establish(self, owner_id, item_id, initializing_run_id, result: DiffResult,
                  skipped: list[Skipped], now: datetime) -> bool:
        """Idempotent: the first normalisation of the seed wins; a repeat changes nothing."""
        with self._write():
            self._item(owner_id, item_id)
            seed = self._db.execute(
                "SELECT baseline FROM watchlist_baselines WHERE owner_id=? AND item_id=?",
                (owner_id, item_id)).fetchone()
            if seed is None or json.loads(seed[0])["initializing_run_id"] != (
                initializing_run_id
            ):
                raise DiffConflict("the Baseline seed is not the one that was normalised")
            if self._initializing_run(owner_id, item_id):
                return False
            detail = {"entries": [e.model_dump(mode="json") for e in result.entries],
                      "skipped": [s.model_dump(mode="json") for s in skipped]}
            self._db.execute(
                "INSERT INTO watchlist_baseline_state VALUES (?,?,?,?,?,?)",
                (owner_id, item_id, initializing_run_id, 1, now.isoformat(),
                 json.dumps(detail)))
            self._upsert(owner_id, item_id, result.updates)
            return True

    def _upsert(self, owner_id, item_id, updates: dict[str, EstablishedFinding]):
        self._db.executemany(
            "INSERT INTO watchlist_findings VALUES (?,?,?,?) "
            "ON CONFLICT(owner_id,item_id,finding_key) DO UPDATE SET state=excluded.state",
            [(owner_id, item_id, key, f.model_dump_json()) for key, f in updates.items()])

    def findings(self, owner_id, item_id) -> tuple[int, dict[str, EstablishedFinding]]:
        """The established state and its version; raises DiffConflict when still a seed."""
        with self._lock:
            self._item(owner_id, item_id)
            row = self._db.execute(
                "SELECT version FROM watchlist_baseline_state WHERE owner_id=? AND item_id=?",
                (owner_id, item_id)).fetchone()
            if row is None:
                raise DiffConflict("the Baseline is not established yet")
            rows = self._db.execute(
                "SELECT finding_key,state FROM watchlist_findings WHERE owner_id=? AND item_id=?",
                (owner_id, item_id)).fetchall()
            return row[0], {k: EstablishedFinding.model_validate_json(s) for k, s in rows}

    # --- ordered Diff lifecycle -------------------------------------------------------------

    def _next_run(self, owner_id, item_id) -> NextDiff | None:
        """The earliest COMPLETE Run after the initializing one whose Diff is unfinished, unless
        an earlier Run of the item is still active (it may yet complete and must go first)."""
        init = self._initializing_run(owner_id, item_id)
        if init is None:
            return None
        row = self._db.execute(
            "SELECT r.id, p.id, r.created_at, d.retry_after FROM runs r "
            "JOIN runs i ON i.owner_id=r.owner_id AND i.id=? "
            "LEFT JOIN reports p ON p.owner_id=r.owner_id AND p.run_id=r.id "
            "LEFT JOIN watchlist_diffs d ON d.owner_id=r.owner_id "
            " AND d.item_id=r.watchlist_item_id AND d.run_id=r.id "
            "WHERE r.owner_id=? AND r.watchlist_item_id=? AND r.state='COMPLETE' "
            "AND (r.created_at, r.id) > (i.created_at, i.id) "
            "AND (d.status IS NULL OR d.status='pending') "
            "ORDER BY r.created_at, r.id LIMIT 1",
            (init, owner_id, item_id)).fetchone()
        if row is None:
            return None
        active = self._db.execute(
            "SELECT 1 FROM runs WHERE owner_id=? AND watchlist_item_id=? "
            "AND state NOT IN ('COMPLETE','FAILED') AND (created_at, id) < (?, ?) LIMIT 1",
            (owner_id, item_id, row[2], row[0])).fetchone()
        if active:
            return None
        return NextDiff(row[0], row[1], datetime.fromisoformat(row[3]) if row[3] else None)

    def next_run(self, owner_id, item_id) -> NextDiff | None:
        with self._lock:
            self._item(owner_id, item_id)
            return self._next_run(owner_id, item_id)

    def begin_attempt(self, owner_id, item_id, run_id, report_id, max_attempts,
                      now: datetime) -> int | None:
        """Durably count one attempt. None when the Diff is already terminal, or when earlier
        attempts were all used (a crash mid-attempt): it is then failed permanently."""
        with self._write():
            self._item(owner_id, item_id)
            self._db.execute(
                "INSERT OR IGNORE INTO watchlist_diffs "
                "(owner_id,item_id,run_id,report_id,status,updated_at) "
                "VALUES (?,?,?,?,'pending',?)",
                (owner_id, item_id, run_id, report_id, now.isoformat()))
            status, attempts = self._db.execute(
                "SELECT status, attempts FROM watchlist_diffs "
                "WHERE owner_id=? AND item_id=? AND run_id=?",
                (owner_id, item_id, run_id)).fetchone()
            if status != "pending":
                return None
            if attempts >= max_attempts:
                self._db.execute(
                    "UPDATE watchlist_diffs SET status='failed', updated_at=?, "
                    "error=CASE WHEN error='' THEN 'attempts exhausted' ELSE error END "
                    "WHERE owner_id=? AND item_id=? AND run_id=?",
                    (now.isoformat(), owner_id, item_id, run_id))
                return None
            self._db.execute(
                "UPDATE watchlist_diffs SET attempts=attempts+1, updated_at=? "
                "WHERE owner_id=? AND item_id=? AND run_id=?",
                (now.isoformat(), owner_id, item_id, run_id))
            return attempts + 1

    def record_failure(self, owner_id, item_id, run_id, error: str,
                       retry_after: datetime | None, now: datetime) -> None:
        """A failed attempt never touches the Baseline. With `retry_after` the Diff stays
        pending until then; without it the failure is permanent. Terminal rows are left."""
        with self._write():
            self._db.execute(
                "UPDATE watchlist_diffs SET error=?, updated_at=?, retry_after=?, "
                "status=CASE WHEN ? IS NULL THEN 'failed' ELSE status END "
                "WHERE owner_id=? AND item_id=? AND run_id=? AND status='pending'",
                (error[:_ERROR_CHARS], now.isoformat(),
                 retry_after.isoformat() if retry_after else None,
                 retry_after.isoformat() if retry_after else None,
                 owner_id, item_id, run_id))

    def intervening_failures(self, owner_id, item_id, run_id) -> list[FailedDiff]:
        """Permanently failed Diffs after the last successful one (or the initializing Run)
        and before this Run: the net change of this Diff spans them."""
        with self._lock:
            self._item(owner_id, item_id)
            init = self._initializing_run(owner_id, item_id)
            rows = self._db.execute(
                "SELECT d.run_id, d.report_id, d.status, d.error FROM watchlist_diffs d "
                "JOIN runs r ON r.owner_id=d.owner_id AND r.id=d.run_id "
                "JOIN runs c ON c.owner_id=d.owner_id AND c.id=? "
                "JOIN runs i ON i.owner_id=d.owner_id AND i.id=? "
                "WHERE d.owner_id=? AND d.item_id=? AND d.status IN ('succeeded','failed') "
                "AND (r.created_at, r.id) < (c.created_at, c.id) "
                "AND (r.created_at, r.id) > (i.created_at, i.id) "
                "ORDER BY r.created_at, r.id",
                (run_id, init, owner_id, item_id)).fetchall()
        failed: list[FailedDiff] = []
        for run, report, status, error in rows:
            if status == "succeeded":
                failed = []
            else:
                failed.append(FailedDiff(run_id=run, report_id=report, error=error))
        return failed

    def commit(self, owner_id, item_id, run_id, expected_version: int, result: DiffResult,
               skipped: list[Skipped], intervening: list[FailedDiff], alert: Alert | None,
               delivery: WebhookDelivery | None, now: datetime) -> bool:
        """One transaction: Finding updates, version, Diff record, Alert and its delivery.
        False when this Diff is already terminal (a duplicate). DiffConflict when the Baseline
        moved or an earlier Diff is unfinished."""
        with self._write():
            self._item(owner_id, item_id)
            row = self._db.execute(
                "SELECT status FROM watchlist_diffs WHERE owner_id=? AND item_id=? AND run_id=?",
                (owner_id, item_id, run_id)).fetchone()
            if row is None:
                raise DiffConflict("no attempt was started for this Diff")
            if row[0] != "pending":
                return False
            version = self._db.execute(
                "SELECT version FROM watchlist_baseline_state WHERE owner_id=? AND item_id=?",
                (owner_id, item_id)).fetchone()
            if version is None or version[0] != expected_version:
                raise DiffConflict("the established Baseline changed during this Diff")
            nxt = self._next_run(owner_id, item_id)
            if nxt is None or nxt.run_id != run_id:
                raise DiffConflict("an earlier Diff of this Watchlist Item is unfinished")
            self._upsert(owner_id, item_id, result.updates)
            self._db.execute(
                "UPDATE watchlist_baseline_state SET version=version+1 "
                "WHERE owner_id=? AND item_id=?", (owner_id, item_id))
            detail = {"entries": [e.model_dump(mode="json") for e in result.entries],
                      "skipped": [s.model_dump(mode="json") for s in skipped],
                      "intervening_failed": [f.model_dump(mode="json") for f in intervening]}
            self._db.execute(
                "UPDATE watchlist_diffs SET status='succeeded', error='', detail=?, alert_id=?, "
                "updated_at=? WHERE owner_id=? AND item_id=? AND run_id=?",
                (json.dumps(detail), alert.id if alert else None, now.isoformat(),
                 owner_id, item_id, run_id))
            if alert is not None:
                self._db.execute(
                    "INSERT INTO watchlist_alerts (owner_id,id,item_id,run_id,created_at,alert) "
                    "VALUES (?,?,?,?,?,?)",
                    (owner_id, alert.id, item_id, run_id, alert.created_at.isoformat(),
                     alert.model_dump_json()))
                if delivery is not None:
                    self._db.execute(
                        "INSERT INTO alert_deliveries "
                        "(owner_id,alert_id,url,status,attempts,next_attempt_at) "
                        "VALUES (?,?,?,'pending',0,?)",
                        (owner_id, alert.id, delivery.url,
                         delivery.next_attempt_at.isoformat()))
            return True

    def diff(self, owner_id, item_id, run_id) -> DiffRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT run_id,report_id,status,attempts,error,detail,alert_id,updated_at "
                "FROM watchlist_diffs WHERE owner_id=? AND item_id=? AND run_id=?",
                (owner_id, item_id, run_id)).fetchone()
        return self._record(owner_id, item_id, row) if row else None

    def diffs(self, owner_id, item_id) -> list[DiffRecord]:
        """The item's Diff log in Run order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT d.run_id,d.report_id,d.status,d.attempts,d.error,d.detail,d.alert_id,"
                "d.updated_at FROM watchlist_diffs d "
                "JOIN runs r ON r.owner_id=d.owner_id AND r.id=d.run_id "
                "WHERE d.owner_id=? AND d.item_id=? ORDER BY r.created_at, r.id",
                (owner_id, item_id)).fetchall()
        return [self._record(owner_id, item_id, r) for r in rows]

    @staticmethod
    def _record(owner_id, item_id, row) -> DiffRecord:
        run_id, report_id, status, attempts, error, detail, alert, updated = row
        detail = json.loads(detail)
        return DiffRecord(
            owner_id=owner_id, watchlist_item_id=item_id, run_id=run_id, report_id=report_id,
            status=status, attempts=attempts, error=error,
            entries=[DiffEntry.model_validate(e) for e in detail.get("entries", [])],
            skipped=[Skipped.model_validate(s) for s in detail.get("skipped", [])],
            intervening_failed=[FailedDiff.model_validate(f)
                                for f in detail.get("intervening_failed", [])],
            alert_id=alert, updated_at=datetime.fromisoformat(updated))

    # --- Alerts and deliveries --------------------------------------------------------------

    def alerts(self, owner_id, item_id=None) -> list[Alert]:
        """Newest first, owner-scoped (T15 reads these)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT alert, read FROM watchlist_alerts WHERE owner_id=? "
                "AND (? IS NULL OR item_id=?) ORDER BY created_at DESC, id DESC",
                (owner_id, item_id, item_id)).fetchall()
        return [Alert.model_validate_json(a).model_copy(update={"read": bool(r)})
                for a, r in rows]

    def alert(self, owner_id, alert_id) -> Alert:
        with self._lock:
            row = self._db.execute(
                "SELECT alert, read FROM watchlist_alerts WHERE owner_id=? AND id=?",
                (owner_id, alert_id)).fetchone()
        if row is None:
            raise NotFoundError(f"alert {alert_id}")
        return Alert.model_validate_json(row[0]).model_copy(update={"read": bool(row[1])})

    def mark_read(self, owner_id, alert_id, read: bool = True) -> None:
        with self._write():
            if self._db.execute(
                "UPDATE watchlist_alerts SET read=? WHERE owner_id=? AND id=?",
                (int(read), owner_id, alert_id)).rowcount == 0:
                raise NotFoundError(f"alert {alert_id}")

    def delivery(self, owner_id, alert_id) -> WebhookDelivery | None:
        with self._lock:
            row = self._db.execute(
                "SELECT owner_id,alert_id,url,status,attempts,next_attempt_at,last_error,"
                "delivered_at FROM alert_deliveries WHERE owner_id=? AND alert_id=?",
                (owner_id, alert_id)).fetchone()
        return self._delivery(row) if row else None

    def pending_deliveries(self) -> list[WebhookDelivery]:
        """System startup only."""
        with self._lock:
            rows = self._db.execute(
                "SELECT owner_id,alert_id,url,status,attempts,next_attempt_at,last_error,"
                "delivered_at FROM alert_deliveries WHERE status='pending' "
                "ORDER BY next_attempt_at").fetchall()
        return [self._delivery(r) for r in rows]

    @staticmethod
    def _delivery(row) -> WebhookDelivery:
        owner, alert, url, status, attempts, next_at, error, delivered = row
        return WebhookDelivery(
            owner_id=owner, alert_id=alert, url=url, status=status, attempts=attempts,
            next_attempt_at=datetime.fromisoformat(next_at), last_error=error,
            delivered_at=datetime.fromisoformat(delivered) if delivered else None)

    def begin_delivery(self, owner_id, alert_id, max_attempts, now: datetime) -> int | None:
        """Durably count one POST before it is sent. None when not pending, or (after a crash
        between counting and recording) when every attempt was already used."""
        with self._write():
            row = self._db.execute(
                "SELECT status, attempts FROM alert_deliveries WHERE owner_id=? AND alert_id=?",
                (owner_id, alert_id)).fetchone()
            if row is None or row[0] != "pending":
                return None
            if row[1] >= max_attempts:
                self._db.execute(
                    "UPDATE alert_deliveries SET status='failed' "
                    "WHERE owner_id=? AND alert_id=?", (owner_id, alert_id))
                return None
            self._db.execute(
                "UPDATE alert_deliveries SET attempts=attempts+1 "
                "WHERE owner_id=? AND alert_id=?", (owner_id, alert_id))
            return row[1] + 1

    def record_delivery(self, owner_id, alert_id, *, delivered: bool, error: str,
                        next_attempt_at: datetime | None, now: datetime) -> None:
        """Delivered, retry at `next_attempt_at`, or (None) failed permanently."""
        status = "delivered" if delivered else ("pending" if next_attempt_at else "failed")
        with self._write():
            self._db.execute(
                "UPDATE alert_deliveries SET status=?, last_error=?, delivered_at=?, "
                "next_attempt_at=COALESCE(?, next_attempt_at) "
                "WHERE owner_id=? AND alert_id=? AND status='pending'",
                (status, error[:_ERROR_CHARS], now.isoformat() if delivered else None,
                 next_attempt_at.isoformat() if next_attempt_at else None, owner_id, alert_id))
