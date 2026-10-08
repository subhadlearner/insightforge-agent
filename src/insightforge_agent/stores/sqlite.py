"""SQLite implementations of the repositories (stdlib `sqlite3`, no ORM).

Every table carries `owner_id`, and every query filters on it."""

import json
import sqlite3
from datetime import datetime

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import (
    Observation,
    Passage,
    Report,
    Run,
    RunEvent,
    RunState,
    Source,
    SubTaskRecord,
    SubTaskStatus,
)
from insightforge_agent.domain.passages import check_passages
from insightforge_agent.domain.plan import SubTask

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    owner_id TEXT NOT NULL, id TEXT NOT NULL, brief TEXT NOT NULL, state TEXT NOT NULL,
    created_at TEXT NOT NULL, watchlist_item_id TEXT,
    PRIMARY KEY (owner_id, id)
);
CREATE TABLE IF NOT EXISTS subtasks (
    owner_id TEXT NOT NULL, run_id TEXT NOT NULL, position INTEGER NOT NULL,
    subtask TEXT NOT NULL, status TEXT NOT NULL, subtask_id TEXT NOT NULL,
    PRIMARY KEY (owner_id, run_id, subtask_id)
);
CREATE TABLE IF NOT EXISTS sources (
    owner_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
    locator TEXT NOT NULL, title TEXT NOT NULL, credibility_score REAL, published_at TEXT,
    PRIMARY KEY (owner_id, id)
);
CREATE TABLE IF NOT EXISTS run_sources (
    owner_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
    PRIMARY KEY (owner_id, run_id, source_id)
);
CREATE TABLE IF NOT EXISTS observations (
    owner_id TEXT NOT NULL, id TEXT NOT NULL, source_id TEXT NOT NULL, run_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, id)
);
CREATE TABLE IF NOT EXISTS split_observations (
    owner_id TEXT NOT NULL, observation_id TEXT NOT NULL,
    PRIMARY KEY (owner_id, observation_id)
);
CREATE TABLE IF NOT EXISTS passages (
    owner_id TEXT NOT NULL, observation_id TEXT NOT NULL, idx INTEGER NOT NULL,
    text TEXT NOT NULL, page INTEGER, section_heading TEXT,
    PRIMARY KEY (owner_id, observation_id, idx)
);
CREATE TABLE IF NOT EXISTS reports (
    owner_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
    created_at TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY (owner_id, id), UNIQUE (owner_id, run_id)
);
CREATE TABLE IF NOT EXISTS run_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, run_id TEXT NOT NULL,
    type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_events_cursor ON run_events (owner_id, run_id, seq);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class _Base:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db


class SqliteRunRepository(_Base):
    def add(self, run: Run) -> None:
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                    (run.owner_id, run.id, run.brief, run.state.value,
                     run.created_at.isoformat(), run.watchlist_item_id),
                )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"run {run.id} already exists") from e

    @staticmethod
    def _row(row) -> Run:
        return Run(owner_id=row[0], id=row[1], brief=row[2], state=RunState(row[3]),
                   created_at=_dt(row[4]), watchlist_item_id=row[5])

    def get(self, owner_id: str, run_id: str) -> Run:
        row = self._db.execute(
            "SELECT * FROM runs WHERE owner_id=? AND id=?", (owner_id, run_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id}")
        return self._row(row)

    def list_for_owner(self, owner_id: str) -> list[Run]:
        rows = self._db.execute(
            "SELECT * FROM runs WHERE owner_id=? ORDER BY created_at, id", (owner_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def set_state(self, owner_id: str, run_id: str, state: RunState) -> Run:
        with self._db:
            cur = self._db.execute(
                "UPDATE runs SET state=? WHERE owner_id=? AND id=?",
                (state.value, owner_id, run_id),
            )
        if cur.rowcount == 0:
            raise NotFoundError(f"run {run_id}")
        return self.get(owner_id, run_id)


class SqliteSubTaskRepository(_Base):
    def save_plan(self, owner_id: str, run_id: str, records: list[SubTaskRecord]) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM subtasks WHERE owner_id=? AND run_id=?", (owner_id, run_id)
            )
            self._db.executemany(
                "INSERT INTO subtasks VALUES (?,?,?,?,?,?)",
                [(owner_id, run_id, i, r.subtask.model_dump_json(), r.status.value, r.subtask.id)
                 for i, r in enumerate(records)],
            )

    def list(self, owner_id: str, run_id: str) -> list[SubTaskRecord]:
        rows = self._db.execute(
            "SELECT subtask, status FROM subtasks WHERE owner_id=? AND run_id=? ORDER BY position",
            (owner_id, run_id),
        ).fetchall()
        return [
            SubTaskRecord(run_id=run_id, subtask=SubTask.model_validate_json(s),
                          status=SubTaskStatus(st))
            for s, st in rows
        ]

    def set_status(
        self, owner_id: str, run_id: str, subtask_id: str, status: SubTaskStatus
    ) -> None:
        with self._db:
            cur = self._db.execute(
                "UPDATE subtasks SET status=? WHERE owner_id=? AND run_id=? AND subtask_id=?",
                (status.value, owner_id, run_id, subtask_id),
            )
        if cur.rowcount == 0:
            raise NotFoundError(f"sub-task {subtask_id}")


class SqliteSourceRepository(_Base):
    @staticmethod
    def _row(row) -> Source:
        return Source(owner_id=row[0], id=row[1], run_id=row[2], kind=row[3], locator=row[4],
                      title=row[5], credibility_score=row[6], published_at=_dt(row[7]))

    def add(self, source: Source) -> Source:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?)",
                (source.owner_id, source.id, source.run_id, source.kind, source.locator,
                 source.title, source.credibility_score, _iso(source.published_at)),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO run_sources VALUES (?,?,?)",
                (source.owner_id, source.run_id, source.id),
            )
        return self.get(source.owner_id, source.id)

    def get(self, owner_id: str, source_id: str) -> Source:
        row = self._db.execute(
            "SELECT * FROM sources WHERE owner_id=? AND id=?", (owner_id, source_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"source {source_id}")
        return self._row(row)

    def list_for_run(self, owner_id: str, run_id: str) -> list[Source]:
        rows = self._db.execute(
            "SELECT s.* FROM sources s JOIN run_sources l "
            "ON l.owner_id=s.owner_id AND l.source_id=s.id "
            "WHERE l.owner_id=? AND l.run_id=? ORDER BY l.rowid",
            (owner_id, run_id),
        ).fetchall()
        return [self._row(r) for r in rows]


class SqliteObservationRepository(_Base):
    @staticmethod
    def _row(row) -> Observation:
        return Observation(owner_id=row[0], id=row[1], source_id=row[2], run_id=row[3],
                           fetched_at=_dt(row[4]))

    def add(self, observation: Observation) -> Observation:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)",
                (observation.owner_id, observation.id, observation.source_id, observation.run_id,
                 observation.fetched_at.isoformat()),
            )
        return self.get(observation.owner_id, observation.id)

    def get(self, owner_id: str, observation_id: str) -> Observation:
        row = self._db.execute(
            "SELECT * FROM observations WHERE owner_id=? AND id=?", (owner_id, observation_id)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"observation {observation_id}")
        return self._row(row)

    def list_for_source(self, owner_id: str, source_id: str) -> list[Observation]:
        rows = self._db.execute(
            "SELECT * FROM observations WHERE owner_id=? AND source_id=? ORDER BY fetched_at, id",
            (owner_id, source_id),
        ).fetchall()
        return [self._row(r) for r in rows]


class SqlitePassageRepository(_Base):
    @staticmethod
    def _row(observation_id: str, row) -> Passage:
        return Passage(observation_id=observation_id, index=row[0], text=row[1], page=row[2],
                       section_heading=row[3])

    def add_all(self, owner_id: str, observation_id: str, passages: list[Passage]) -> None:
        check_passages(observation_id, passages)
        with self._db:  # marker and Passages commit together or not at all
            marked = self._db.execute(
                "INSERT OR IGNORE INTO split_observations VALUES (?,?)",
                (owner_id, observation_id),
            ).rowcount
            if not marked:
                return  # already split, possibly into nothing: never re-split
            self._db.executemany(
                "INSERT INTO passages VALUES (?,?,?,?,?,?)",
                [(owner_id, observation_id, p.index, p.text, p.page, p.section_heading)
                 for p in passages],
            )

    def list(self, owner_id: str, observation_id: str) -> list[Passage]:
        rows = self._db.execute(
            "SELECT idx, text, page, section_heading FROM passages "
            "WHERE owner_id=? AND observation_id=? ORDER BY idx",
            (owner_id, observation_id),
        ).fetchall()
        return [self._row(observation_id, r) for r in rows]

    def get(self, owner_id: str, observation_id: str, index: int) -> Passage:
        row = self._db.execute(
            "SELECT idx, text, page, section_heading FROM passages "
            "WHERE owner_id=? AND observation_id=? AND idx=?",
            (owner_id, observation_id, index),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"passage {observation_id}#{index}")
        return self._row(observation_id, row)


class SqliteReportRepository(_Base):
    @staticmethod
    def _row(row) -> Report:
        return Report(owner_id=row[0], id=row[1], run_id=row[2], created_at=_dt(row[3]),
                      body=json.loads(row[4]))

    def add(self, report: Report) -> None:
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO reports VALUES (?,?,?,?,?)",
                    (report.owner_id, report.id, report.run_id, report.created_at.isoformat(),
                     json.dumps(report.body)),
                )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"run {report.run_id} already has a Report") from e

    def get(self, owner_id: str, report_id: str) -> Report:
        row = self._db.execute(
            "SELECT owner_id, id, run_id, created_at, body FROM reports WHERE owner_id=? AND id=?",
            (owner_id, report_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"report {report_id}")
        return self._row(row)

    def get_for_run(self, owner_id: str, run_id: str) -> Report:
        row = self._db.execute(
            "SELECT owner_id, id, run_id, created_at, body FROM reports "
            "WHERE owner_id=? AND run_id=?",
            (owner_id, run_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"report for run {run_id}")
        return self._row(row)

    def list_for_owner(self, owner_id: str) -> list[Report]:
        rows = self._db.execute(
            "SELECT owner_id, id, run_id, created_at, body FROM reports "
            "WHERE owner_id=? ORDER BY created_at, id",
            (owner_id,),
        ).fetchall()
        return [self._row(r) for r in rows]


class SqliteRunEventRepository(_Base):
    def append(self, event: RunEvent) -> RunEvent:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO run_events (owner_id, run_id, type, payload, created_at) "
                "VALUES (?,?,?,?,?)",
                (event.owner_id, event.run_id, event.type, json.dumps(event.payload),
                 event.created_at.isoformat()),
            )
        return event.model_copy(update={"seq": cur.lastrowid})

    def read_after(
        self, owner_id: str, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[RunEvent]:
        rows = self._db.execute(
            "SELECT seq, owner_id, run_id, type, payload, created_at FROM run_events "
            "WHERE owner_id=? AND run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (owner_id, run_id, after_seq, limit),
        ).fetchall()
        return [
            RunEvent(seq=r[0], owner_id=r[1], run_id=r[2], type=r[3], payload=json.loads(r[4]),
                     created_at=_dt(r[5]))
            for r in rows
        ]


def sqlite_path(database_url: str) -> str:
    """`sqlite:///rel.db` -> `rel.db`; `sqlite:////abs/x.db` -> `/abs/x.db`; `sqlite://` -> memory."""
    prefix = "sqlite://"
    if not database_url.startswith(prefix):
        raise ValueError(f"not a sqlite URL: {database_url}")
    path = database_url[len(prefix):]
    if path.startswith("/"):
        path = path[1:]
    return path or ":memory:"


class SqliteRepos:
    """All repositories over one SQLite file (or ":memory:")."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self.runs = SqliteRunRepository(self._db)
        self.subtasks = SqliteSubTaskRepository(self._db)
        self.sources = SqliteSourceRepository(self._db)
        self.observations = SqliteObservationRepository(self._db)
        self.passages = SqlitePassageRepository(self._db)
        self.reports = SqliteReportRepository(self._db)
        self.events = SqliteRunEventRepository(self._db)

    def close(self) -> None:
        self._db.close()
