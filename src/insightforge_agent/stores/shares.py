"""Durable T15 Report-share records used to revoke otherwise-valid JWTs."""

from contextlib import contextmanager
from datetime import datetime

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.sharing import ShareRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_shares (
 owner_id TEXT NOT NULL,
 id TEXT NOT NULL,
 report_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 revoked_at TEXT,
 PRIMARY KEY(owner_id,id)
);
CREATE INDEX IF NOT EXISTS report_shares_report
 ON report_shares(owner_id,report_id,created_at);
"""


class SqliteShareRepository:
    def __init__(self, db, lock):
        self._db, self._lock = db, lock

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                yield

    @staticmethod
    def _record(row) -> ShareRecord:
        return ShareRecord(
            owner_id=row[0], id=row[1], report_id=row[2],
            created_at=datetime.fromisoformat(row[3]),
            expires_at=datetime.fromisoformat(row[4]),
            revoked_at=datetime.fromisoformat(row[5]) if row[5] else None,
        )

    def add(self, record: ShareRecord) -> None:
        with self._write():
            if self._db.execute(
                "SELECT 1 FROM reports WHERE owner_id=? AND id=?",
                (record.owner_id, record.report_id),
            ).fetchone() is None:
                raise NotFoundError(f"report {record.report_id}")
            self._db.execute(
                "INSERT INTO report_shares VALUES (?,?,?,?,?,NULL)",
                (record.owner_id, record.id, record.report_id,
                 record.created_at.isoformat(), record.expires_at.isoformat()),
            )

    def get(self, owner_id: str, share_id: str) -> ShareRecord:
        with self._lock:
            row = self._db.execute(
                "SELECT owner_id,id,report_id,created_at,expires_at,revoked_at "
                "FROM report_shares WHERE owner_id=? AND id=?",
                (owner_id, share_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"share {share_id}")
        return self._record(row)

    def list_for_report(self, owner_id: str, report_id: str, now: datetime) -> list[ShareRecord]:
        """Only currently valid shares for one owner-scoped Report, in stable order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT owner_id,id,report_id,created_at,expires_at,revoked_at "
                "FROM report_shares "
                "WHERE owner_id=? AND report_id=? AND revoked_at IS NULL "
                "ORDER BY created_at,id",
                (owner_id, report_id),
            ).fetchall()
        return [r for row in rows if (r := self._record(row)).expires_at > now]

    def revoke(self, owner_id: str, report_id: str, share_id: str, revoked_at: datetime) -> None:
        with self._write():
            row = self._db.execute(
                "SELECT report_id,revoked_at FROM report_shares WHERE owner_id=? AND id=?",
                (owner_id, share_id),
            ).fetchone()
            if row is None or row[0] != report_id:
                raise NotFoundError(f"share {share_id}")
            if row[1] is None:
                self._db.execute(
                    "UPDATE report_shares SET revoked_at=? WHERE owner_id=? AND id=?",
                    (revoked_at.isoformat(), owner_id, share_id),
                )
