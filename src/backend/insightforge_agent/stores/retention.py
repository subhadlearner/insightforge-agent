"""SQLite side of the retention sweep (design.md section 6, Retention; rules in
`domain/retention.py`).

A sweep works per User in bounded batches:

1. `protection` reads the User's retained records under the shared lock and interprets them
   outside it; `plan` (read only, under the lock) uses that to pick the next unprotected raw
   material of their expired terminal Runs, about `limit` Passages.
2. `apply` (one `BEGIN IMMEDIATE` transaction) evaluates eligibility and protection again from
   current rows, removes the uploaded-document chunks that are still removable from
   `doc_chunks` (through the caller's `remove_chunks`), then deletes only what is still
   eligible and unprotected. Another repository write (a new Report, a Diff commit, a Run's
   fetch) is either before it, and then seen, or after it.

Qdrant shares no transaction with SQLite. A Qdrant failure keeps those chunks' Passages (the
Observations are returned as `skipped`); a failure after Qdrant and before the commit leaves
SQLite unchanged, and the next sweep removes the same chunks again (a missing point is not an
error). Because the chunks are removed after the protection check, a chunk that became
protected is never removed from Qdrant. The price is one bounded Qdrant call per batch while
holding the lock.

What `apply` removes, for an expired Run only:

- Passages nothing retained refers to. A web Observation is cleaned Passage by Passage, so an
  uncited paragraph goes while a cited one stays. An uploaded document's Observation goes only
  whole, and is kept whole while any of its chunks is referenced, because a document Claim is
  fact-checked against every stored chunk of its Observation (`document_passages`).
- An Observation (and its split marker) once it has no Passages left and nothing refers to it.
- The Run's Sub-task rows, its links to Sources it no longer holds an Observation of (a link to
  retained material stays, so `list_for_run` still lists the Sources behind a Report's
  citations), and its progress events, except the stage trail and the failure/recovery events,
  which stay as the Run's compact history.
- A Source with no Observation, no Run link and no reference left. `sources.run_id` is only the
  first Run that fetched it, never who owns or may delete it.

Extracted facts are never removed: a fact whose Passage goes is kept with its provenance marked
unavailable. Runs, Reports, Run summaries, failure records, Baseline seeds, established
Findings, the Diff log and Alerts are never touched."""

import json
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime

from insightforge_agent.domain.contracts import ExtractedFact, PassageRef
from insightforge_agent.domain.models import Run
from insightforge_agent.domain.retention import (
    MAX_REASON_CHARS,
    FailureRecord,
    MalformedRetentionRoot,
    ProtectedRefs,
    expired,
)
from insightforge_agent.stores.run_rows import run_from_row

# Progress events an expired Run keeps: the stage trail and how it ended.
KEPT_EVENT_TYPES = ("stage", "run_failed", "completion_recovered")
_KEPT = ",".join("?" * len(KEPT_EVENT_TYPES))
LEGACY_REASON = "the Run failed before failure records were kept; its reason was not recorded"
# A Run-to-Source link that may go: the Run no longer holds an Observation of that Source. A
# link to material the Run still holds stays, so `SourceRepository.list_for_run` keeps listing
# the Sources of an expired Run's retained (cited) material.
_DISPOSABLE_LINK = ("NOT EXISTS (SELECT 1 FROM observations o WHERE o.owner_id=l.owner_id "
                    "AND o.run_id=l.run_id AND o.source_id=l.source_id)")

# Removes one Run's chunks from doc_chunks: (owner_id, run_id, refs). Raises on failure.
RemoveChunks = Callable[[str, str, list[PassageRef]], None]

# (kind of record, query for (key, JSON) of one User's records, how ProtectedRefs reads it)
_ROOTS = (
    ("report", "SELECT id, body FROM reports WHERE owner_id=?", "add_report"),
    ("Baseline seed", "SELECT item_id, baseline FROM watchlist_baselines WHERE owner_id=?",
     "add_baseline_seed"),
    ("established Finding", "SELECT item_id || '/' || finding_key, state "
     "FROM watchlist_findings WHERE owner_id=?", "add_established"),
    ("Baseline normalisation", "SELECT item_id, detail FROM watchlist_baseline_state "
     "WHERE owner_id=?", "add_diff_detail"),
    ("Diff", "SELECT item_id || '/' || run_id, detail FROM watchlist_diffs WHERE owner_id=?",
     "add_diff_detail"),
    ("Alert", "SELECT id, alert FROM watchlist_alerts WHERE owner_id=?", "add_alert"),
)


def _utc(value: str) -> datetime:
    """Stored timestamps are ISO strings; a naive one is read as UTC."""
    at = datetime.fromisoformat(value)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _uploaded(kind: str | None) -> bool:
    """Not a web page: its chunks may be in doc_chunks, and it goes only whole. A Source of
    unknown kind is treated the same way, the cautious choice."""
    return kind != "web"


@dataclass(frozen=True)
class ObservationPlan:
    observation_id: str
    run_id: str  # the Run that fetched it, which also scopes its doc_chunks points
    uploaded: bool
    indices: tuple[int, ...]  # the Passages to remove (all of them for an uploaded document)


@dataclass
class RetentionBatch:
    owner_id: str
    runs: list[str] = field(default_factory=list)  # expired Runs this batch touches
    observations: list[ObservationPlan] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.runs and not self.observations


@dataclass
class Applied:
    """What one batch removed. `skipped`: uploaded Observations whose chunks Qdrant could not
    remove (kept, and left out of the rest of this sweep)."""

    passages: int = 0
    observations: int = 0
    sources: int = 0
    facts_detached: int = 0
    subtasks: int = 0
    events: int = 0
    run_links: int = 0
    chunk_points_removed: int = 0
    chunk_removal_failed: int = 0
    skipped: set[str] = field(default_factory=set)

    def counts(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "skipped"}

    @property
    def changed(self) -> int:
        return sum(v for k, v in self.counts().items() if k != "chunk_removal_failed")


class SqliteRetentionRepository:
    def __init__(self, db, lock):
        self._db, self._lock = db, lock

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                yield

    # --- failure records ---------------------------------------------------------------------

    def backfill_failures(self, now: datetime) -> int:
        """Give every FAILED Run without a failure record (one that failed before records were
        kept) a backfilled record whose retention starts `now`. System-wide: for the sweep."""
        with self._write():
            rows = self._db.execute(
                "SELECT r.* FROM runs r LEFT JOIN run_failures f "
                "ON f.owner_id=r.owner_id AND f.run_id=r.id "
                "WHERE r.state='FAILED' AND f.run_id IS NULL").fetchall()
            for run in map(run_from_row, rows):
                record = FailureRecord.legacy(run, reason=self._logged_reason(run) or LEGACY_REASON,
                                              backfilled_at=now)
                self._db.execute(
                    "INSERT OR IGNORE INTO run_failures VALUES (?,?,?,?)",
                    (run.owner_id, run.id, now.isoformat(), record.model_dump_json()))
        return len(rows)

    def _logged_reason(self, run: Run) -> str | None:
        row = self._db.execute(
            "SELECT payload FROM run_events WHERE owner_id=? AND run_id=? AND type='run_failed' "
            "ORDER BY seq DESC LIMIT 1", (run.owner_id, run.id)).fetchone()
        reason = json.loads(row[0]).get("reason") if row else None
        return reason[:MAX_REASON_CHARS] if isinstance(reason, str) and reason else None

    # --- eligibility -------------------------------------------------------------------------

    def expired_runs(self, cutoff: datetime) -> list[tuple[str, str]]:
        """Every User's terminal Runs whose terminal timestamp is at or before `cutoff`, as
        (owner, Run), oldest first. System-wide: for the sweep only, which then works per
        User. A COMPLETE Run without a Report, or a FAILED Run without a failure record, has
        no terminal timestamp and is not listed."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.owner_id, r.id, p.created_at FROM runs r JOIN reports p "
                "ON p.owner_id=r.owner_id AND p.run_id=r.id WHERE r.state='COMPLETE' "
                "UNION ALL "
                "SELECT r.owner_id, r.id, f.retention_starts_at FROM runs r JOIN run_failures f "
                "ON f.owner_id=r.owner_id AND f.run_id=r.id WHERE r.state='FAILED'").fetchall()
        terminal = sorted((_utc(at), owner, run) for owner, run, at in rows)
        return [(owner, run) for at, owner, run in terminal if expired(at, cutoff)]

    def _still_expired(self, owner_id: str, run_id: str, cutoff: datetime) -> bool:
        row = self._db.execute(
            "SELECT CASE r.state "
            " WHEN 'COMPLETE' THEN (SELECT p.created_at FROM reports p "
            "                       WHERE p.owner_id=r.owner_id AND p.run_id=r.id) "
            " WHEN 'FAILED' THEN (SELECT f.retention_starts_at FROM run_failures f "
            "                     WHERE f.owner_id=r.owner_id AND f.run_id=r.id) END "
            "FROM runs r WHERE r.owner_id=? AND r.id=?", (owner_id, run_id)).fetchone()
        return bool(row and row[0]) and expired(_utc(row[0]), cutoff)

    # --- protection --------------------------------------------------------------------------

    def protection(self, owner_id: str) -> ProtectedRefs:
        """What the User's retained records refer to: read under the lock, interpreted outside
        it. Raises MalformedRetentionRoot when one cannot be read, so the caller removes
        nothing of this User."""
        with self._lock:
            rows = self._root_rows(owner_id)
        return _interpret(rows)

    def _root_rows(self, owner_id: str) -> list[tuple[str, str, str, str]]:
        return [(kind, key, raw, add) for kind, sql, add in _ROOTS
                for key, raw in self._db.execute(sql, (owner_id,))]

    # --- one batch ---------------------------------------------------------------------------

    def plan(self, owner_id: str, run_ids: list[str], limit: int, protected: ProtectedRefs,
             skip: set[str] | frozenset[str] = frozenset()) -> RetentionBatch:
        """The next raw material of these expired Runs that `protected` does not cover (about
        `limit` Passages; an uploaded document is never split, so one may exceed it), and the
        Runs whose operational detail is still to clean. Observations in `skip` are left out.
        Reads only; `apply` checks everything again."""
        with self._lock:
            batch, budget = RetentionBatch(owner_id), limit
            for run_id in run_ids:
                if budget <= 0:
                    break
                if self._has_operational_detail(owner_id, run_id):
                    batch.runs.append(run_id)
                for plan in self._observation_plans(owner_id, run_id, protected, skip):
                    if budget <= 0:
                        break
                    if not plan.uploaded:
                        plan = ObservationPlan(plan.observation_id, plan.run_id, False,
                                               plan.indices[:budget])
                    budget -= max(1, len(plan.indices))
                    batch.observations.append(plan)
                    if run_id not in batch.runs:
                        batch.runs.append(run_id)
            return batch

    def _has_operational_detail(self, owner_id: str, run_id: str) -> bool:
        return any(self._db.execute(sql, args).fetchone() for sql, args in (
            ("SELECT 1 FROM subtasks WHERE owner_id=? AND run_id=? LIMIT 1", (owner_id, run_id)),
            (f"SELECT 1 FROM run_sources l WHERE owner_id=? AND run_id=? "
             f"AND {_DISPOSABLE_LINK} LIMIT 1", (owner_id, run_id)),
            (f"SELECT 1 FROM run_events WHERE owner_id=? AND run_id=? "
             f"AND type NOT IN ({_KEPT}) LIMIT 1", (owner_id, run_id, *KEPT_EVENT_TYPES)),
        ))

    def _observation_plans(self, owner_id: str, run_id: str, protected: ProtectedRefs,
                           skip: set[str] | frozenset[str]) -> list[ObservationPlan]:
        rows = self._db.execute(
            "SELECT o.id, s.kind FROM observations o LEFT JOIN sources s "
            "ON s.owner_id=o.owner_id AND s.id=o.source_id "
            "WHERE o.owner_id=? AND o.run_id=? ORDER BY o.fetched_at, o.id",
            (owner_id, run_id)).fetchall()
        plans = []
        for oid, kind in rows:
            if oid in skip:
                continue
            indices = self._removable(owner_id, oid, _uploaded(kind), protected)
            if indices is not None:
                plans.append(ObservationPlan(oid, run_id, _uploaded(kind), tuple(indices)))
        return plans

    def _removable(self, owner_id: str, observation_id: str, uploaded: bool,
                   protected: ProtectedRefs) -> list[int] | None:
        """The Passages of an Observation that may go, or None when nothing of it may (it is
        protected, or nothing is left to remove)."""
        if uploaded and observation_id in protected.observations:
            return None
        indices = [i for (i,) in self._db.execute(
            "SELECT idx FROM passages WHERE owner_id=? AND observation_id=? ORDER BY idx",
            (owner_id, observation_id))]
        removable = [i for i in indices if not protected.protects_passage(observation_id, i)]
        if removable:
            return removable
        if not indices and observation_id not in protected.observations:
            return []  # an Observation with no Passages left: only its row goes
        return None

    def apply(self, owner_id: str, batch: RetentionBatch, cutoff: datetime,
              remove_chunks: RemoveChunks | None) -> Applied:
        """Delete what the batch planned that is, now, still eligible and unprotected, in one
        transaction. Uploaded-document chunks are removed from Qdrant through `remove_chunks`
        after that check and before the delete; without it (no index wired) they are kept.
        Raises MalformedRetentionRoot (nothing deleted) as `protection` does."""
        done = Applied()
        with self._write():
            runs = [r for r in batch.runs if self._still_expired(owner_id, r, cutoff)]
            protected = _interpret(self._root_rows(owner_id))
            plans = [p for p in (self._recheck(owner_id, p, protected)
                                 for p in batch.observations if p.run_id in runs) if p]
            done.skipped = self._remove_chunks(owner_id, plans, remove_chunks, done)
            touched_sources: set[str] = set()
            for plan in plans:
                if plan.observation_id not in done.skipped:
                    self._delete_observation(owner_id, plan, protected, touched_sources, done)
            for run_id in runs:
                self._clean_run(owner_id, run_id, touched_sources, done)
            for source_id in sorted(touched_sources - protected.sources):
                if self._source_unused(owner_id, source_id):
                    self._db.execute("DELETE FROM sources WHERE owner_id=? AND id=?",
                                     (owner_id, source_id))
                    done.sources += 1
        return done

    def _recheck(self, owner_id: str, plan: ObservationPlan,
                 protected: ProtectedRefs) -> ObservationPlan | None:
        """The plan as current rows and references allow, or None. An uploaded document is
        still all or nothing."""
        oid = plan.observation_id
        row = self._db.execute("SELECT run_id FROM observations WHERE owner_id=? AND id=?",
                               (owner_id, oid)).fetchone()
        if row is None or row[0] != plan.run_id:
            return None  # gone, or not the Run's Observation: never touched
        if plan.uploaded:
            current = self._removable(owner_id, oid, True, protected)
            return None if current is None else ObservationPlan(oid, plan.run_id, True,
                                                                tuple(current))
        kept = [i for i in plan.indices if not protected.protects_passage(oid, i)]
        if not kept and plan.indices:
            return None
        return ObservationPlan(oid, plan.run_id, False, tuple(kept))

    def _remove_chunks(self, owner_id: str, plans: list[ObservationPlan],
                       remove_chunks: RemoveChunks | None, done: Applied) -> set[str]:
        """Remove the uploaded chunks of these plans from doc_chunks, per fetching Run.
        Returns the Observations whose chunks could not be removed."""
        by_run: dict[str, list[ObservationPlan]] = defaultdict(list)
        for plan in plans:
            if plan.uploaded and plan.indices:
                by_run[plan.run_id].append(plan)
        failed: set[str] = set()
        for run_id, run_plans in by_run.items():
            refs = [PassageRef(observation_id=p.observation_id, index=i)
                    for p in run_plans for i in p.indices]
            try:
                if remove_chunks is None:
                    raise RuntimeError("no doc_chunks index is wired")
                remove_chunks(owner_id, run_id, refs)
            except Exception:  # noqa: BLE001 - Qdrant unavailable: keep these Passages
                failed |= {p.observation_id for p in run_plans}
                done.chunk_removal_failed += len(refs)
            else:
                done.chunk_points_removed += len(refs)
        return failed

    def _delete_observation(self, owner_id: str, plan: ObservationPlan,
                            protected: ProtectedRefs, touched_sources: set[str],
                            done: Applied) -> None:
        oid = plan.observation_id
        if plan.indices:
            done.facts_detached += self._detach_facts(owner_id, oid, set(plan.indices))
            done.passages += self._db.executemany(
                "DELETE FROM passages WHERE owner_id=? AND observation_id=? AND idx=?",
                [(owner_id, oid, i) for i in plan.indices]).rowcount
        if oid in protected.observations or self._db.execute(
                "SELECT 1 FROM passages WHERE owner_id=? AND observation_id=? LIMIT 1",
                (owner_id, oid)).fetchone():
            return  # referenced, or cited siblings remain: the Observation stays
        (source_id,) = self._db.execute(
            "SELECT source_id FROM observations WHERE owner_id=? AND id=?",
            (owner_id, oid)).fetchone()
        self._db.execute("DELETE FROM split_observations WHERE owner_id=? AND observation_id=?",
                         (owner_id, oid))
        done.observations += self._db.execute(
            "DELETE FROM observations WHERE owner_id=? AND id=?", (owner_id, oid)).rowcount
        touched_sources.add(source_id)

    def _detach_facts(self, owner_id: str, observation_id: str, indices: set[int]) -> int:
        """Keep every fact from these Passages, with its provenance marked unavailable. A fact
        row that cannot be read fails the batch: its Passage is never removed under it."""
        rows = self._db.execute(
            "SELECT rowid, fact FROM entities WHERE owner_id=? "
            "AND json_extract(fact, '$.passage.observation_id')=?",
            (owner_id, observation_id)).fetchall()
        detached = 0
        for rowid, raw in rows:
            try:
                fact = ExtractedFact.model_validate_json(raw)
            except ValueError as error:
                raise MalformedRetentionRoot(f"extracted fact row {rowid}") from error
            if fact.passage is not None and fact.passage.index in indices:
                self._db.execute("UPDATE entities SET fact=? WHERE rowid=?",
                                 (fact.without_provenance().model_dump_json(), rowid))
                detached += 1
        return detached

    def _clean_run(self, owner_id: str, run_id: str, touched_sources: set[str],
                   done: Applied) -> None:
        """An expired Run's operational detail. Its Run row, Report, summary and failure
        record stay."""
        disposable = [s for (s,) in self._db.execute(
            f"SELECT source_id FROM run_sources l WHERE owner_id=? AND run_id=? "
            f"AND {_DISPOSABLE_LINK}", (owner_id, run_id))]
        touched_sources.update(disposable)
        done.run_links += self._db.executemany(
            "DELETE FROM run_sources WHERE owner_id=? AND run_id=? AND source_id=?",
            [(owner_id, run_id, s) for s in disposable]).rowcount
        done.subtasks += self._db.execute(
            "DELETE FROM subtasks WHERE owner_id=? AND run_id=?", (owner_id, run_id)).rowcount
        done.events += self._db.execute(
            f"DELETE FROM run_events WHERE owner_id=? AND run_id=? AND type NOT IN ({_KEPT})",
            (owner_id, run_id, *KEPT_EVENT_TYPES)).rowcount

    def _source_unused(self, owner_id: str, source_id: str) -> bool:
        """No Observation (of any Run) and no Run link: a Run still fetching it links it in
        the same write that adds it (`SourceRepository.add`)."""
        return not any(self._db.execute(sql, (owner_id, source_id)).fetchone() for sql in (
            "SELECT 1 FROM observations WHERE owner_id=? AND source_id=? LIMIT 1",
            "SELECT 1 FROM run_sources WHERE owner_id=? AND source_id=? LIMIT 1",
        ))


def _interpret(rows: list[tuple[str, str, str, str]]) -> ProtectedRefs:
    refs = ProtectedRefs()
    for kind, key, raw, add in rows:
        what = f"{kind} {key}"
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise MalformedRetentionRoot(f"{what}: not JSON") from error
        getattr(refs, add)(value, what)
    return refs
