"""SQLite side of the retention sweep (design.md section 6, Retention; rules in
`domain/retention.py`).

A sweep works per User in bounded batches. Each batch is two short steps on the shared lock:

1. `plan` (read only): the User's protected references, and the next unprotected raw material
   of their expired terminal Runs, at most `limit` Passages.
2. `apply` (one `BEGIN IMMEDIATE` transaction): eligibility and protection are evaluated again
   from current rows, and only what is still eligible and unprotected is deleted. Another
   repository write (a new Report, a Diff commit, a Run's fetch) is either before it, and then
   seen, or after it.

The caller removes an uploaded document's `doc_chunks` points between the two (Qdrant shares no
transaction with SQLite): a failed Qdrant call means `apply` is not called for those chunks,
and a failure after it is repaired by the next sweep, which plans the same chunks again
(deleting a missing point is not an error).

What `apply` removes, for an expired Run only:

- Passages nothing retained refers to. A web Observation is cleaned Passage by Passage, so an
  uncited paragraph goes while a cited one stays. An uploaded document's Observation is kept
  whole while any of its chunks is referenced, because a document Claim is fact-checked against
  every stored chunk of its Observation (`document_passages`).
- An Observation (and its split marker) once it has no Passages left and nothing refers to it.
- The Run's Sub-task rows, its Run-to-Source links and its progress events, except the stage
  trail and the failure/recovery events, which stay as the Run's compact history.
- A Source with no Observation, no Run link and no reference left. `sources.run_id` is only the
  first Run that fetched it, never who owns or may delete it.

Extracted facts are never removed: a fact whose Passage goes is kept with its provenance marked
unavailable. Runs, Reports, Run summaries, failure records, Baseline seeds, established
Findings, the Diff log and Alerts are never touched."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from insightforge_agent.domain.contracts import ExtractedFact
from insightforge_agent.domain.models import Run
from insightforge_agent.domain.retention import (
    LEGACY_UNRECORDED,
    FailureRecord,
    MalformedRetentionRoot,
    ProtectedRefs,
    expired,
)
from insightforge_agent.stores.run_rows import run_from_row

# Progress events an expired Run keeps: the stage trail and how it ended.
KEPT_EVENT_TYPES = ("stage", "run_failed", "completion_recovered")
LEGACY_REASON = "the Run failed before failure records were kept; its reason was not recorded"


def _utc(value: str) -> datetime:
    """Stored timestamps are ISO strings; a naive one is read as UTC."""
    at = datetime.fromisoformat(value)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ObservationPlan:
    observation_id: str
    run_id: str  # the Run that fetched it, which also scopes its doc_chunks points
    document: bool  # not a web page: its chunks may be in doc_chunks, and it goes only whole
    indices: tuple[int, ...]  # the Passages to remove in this batch


@dataclass
class RetentionBatch:
    owner_id: str
    runs: list[str] = field(default_factory=list)  # expired Runs this batch touches
    observations: list[ObservationPlan] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.runs and not self.observations

    def without(self, observation_ids: set[str]) -> "RetentionBatch":
        return RetentionBatch(self.owner_id, list(self.runs), [
            o for o in self.observations if o.observation_id not in observation_ids])


@dataclass
class Applied:
    passages: int = 0
    observations: int = 0
    sources: int = 0
    facts_detached: int = 0
    subtasks: int = 0
    events: int = 0
    run_links: int = 0

    @property
    def changed(self) -> int:
        return (self.passages + self.observations + self.sources + self.facts_detached
                + self.subtasks + self.events + self.run_links)


class SqliteRetentionRepository:
    def __init__(self, db, lock):
        self._db, self._lock = db, lock

    # --- failure records ---------------------------------------------------------------------

    def backfill_failures(self, now: datetime) -> int:
        """Give every FAILED Run without a failure record (one that failed before records were
        kept) a backfilled record. Its failure time is unknown, so retention starts `now`:
        never earlier, so nothing expires sooner than the retention period allows."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                rows = self._db.execute(
                    "SELECT r.* FROM runs r LEFT JOIN run_failures f "
                    "ON f.owner_id=r.owner_id AND f.run_id=r.id "
                    "WHERE r.state='FAILED' AND f.run_id IS NULL").fetchall()
                for run in map(run_from_row, rows):
                    record = FailureRecord(
                        owner_id=run.owner_id, run_id=run.id, run_created_at=run.created_at,
                        failed_at=None, retention_starts_at=now, stage=None,
                        reason=self._logged_reason(run) or LEGACY_REASON,
                        error_codes=[LEGACY_UNRECORDED], backfilled=True)
                    self._db.execute(
                        "INSERT OR IGNORE INTO run_failures VALUES (?,?,?,?)",
                        (run.owner_id, run.id, now.isoformat(), record.model_dump_json()))
        return len(rows)

    def _logged_reason(self, run: Run) -> str | None:
        row = self._db.execute(
            "SELECT payload FROM run_events WHERE owner_id=? AND run_id=? AND type='run_failed' "
            "ORDER BY seq DESC LIMIT 1", (run.owner_id, run.id)).fetchone()
        if row is None:
            return None
        reason = json.loads(row[0]).get("reason")
        return reason[:1000] if isinstance(reason, str) and reason else None

    # --- eligibility -------------------------------------------------------------------------

    def expired_runs(self, cutoff: datetime) -> list[tuple[str, str]]:
        """Every User's terminal Runs whose terminal timestamp is at or before `cutoff`, as
        (owner, Run), oldest first. System-wide: for the sweep only. A COMPLETE Run without a
        Report, or a FAILED Run without a failure record, has no terminal timestamp and is
        not listed."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.owner_id, r.id, p.created_at FROM runs r JOIN reports p "
                "ON p.owner_id=r.owner_id AND p.run_id=r.id WHERE r.state='COMPLETE' "
                "UNION ALL "
                "SELECT r.owner_id, r.id, f.retention_starts_at FROM runs r JOIN run_failures f "
                "ON f.owner_id=r.owner_id AND f.run_id=r.id WHERE r.state='FAILED'").fetchall()
        done = sorted((_utc(at), owner, run) for owner, run, at in rows)
        return [(owner, run) for at, owner, run in done if expired(at, cutoff)]

    def _terminal_at(self, owner_id: str, run_id: str) -> datetime | None:
        row = self._db.execute(
            "SELECT CASE r.state "
            " WHEN 'COMPLETE' THEN (SELECT p.created_at FROM reports p "
            "                       WHERE p.owner_id=r.owner_id AND p.run_id=r.id) "
            " WHEN 'FAILED' THEN (SELECT f.retention_starts_at FROM run_failures f "
            "                     WHERE f.owner_id=r.owner_id AND f.run_id=r.id) END "
            "FROM runs r WHERE r.owner_id=? AND r.id=?", (owner_id, run_id)).fetchone()
        return _utc(row[0]) if row and row[0] else None

    def _still_expired(self, owner_id: str, run_id: str, cutoff: datetime) -> bool:
        at = self._terminal_at(owner_id, run_id)
        return at is not None and expired(at, cutoff)

    # --- protection --------------------------------------------------------------------------

    def protection(self, owner_id: str) -> ProtectedRefs:
        """What the User's retained records refer to. Raises MalformedRetentionRoot when one of
        them cannot be read, so the caller removes nothing of this User."""
        with self._lock:
            return self._protection(owner_id)

    def _protection(self, owner_id: str) -> ProtectedRefs:
        refs = ProtectedRefs()
        roots = (
            ("report", "SELECT id, body FROM reports WHERE owner_id=?", refs.add_report),
            ("Baseline seed", "SELECT item_id, baseline FROM watchlist_baselines "
             "WHERE owner_id=?", refs.add_baseline_seed),
            ("established Finding", "SELECT item_id || '/' || finding_key, state "
             "FROM watchlist_findings WHERE owner_id=?", refs.add_established),
            ("Baseline normalisation", "SELECT item_id, detail FROM watchlist_baseline_state "
             "WHERE owner_id=?", refs.add_diff_detail),
            ("Diff", "SELECT item_id || '/' || run_id, detail FROM watchlist_diffs "
             "WHERE owner_id=?", refs.add_diff_detail),
            ("Alert", "SELECT id, alert FROM watchlist_alerts WHERE owner_id=?",
             refs.add_alert),
        )
        for kind, sql, add in roots:
            for key, raw in self._db.execute(sql, (owner_id,)):
                what = f"{kind} {key}"
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError) as error:
                    raise MalformedRetentionRoot(f"{what}: not JSON") from error
                add(value, what)
        return refs

    # --- one batch ---------------------------------------------------------------------------

    def plan(self, owner_id: str, run_ids: list[str], limit: int,
             skip: set[str] = frozenset()) -> RetentionBatch:
        """The next unprotected raw material of these expired Runs (at most `limit` Passages)
        and the Runs whose operational detail is still to clean. Observations in `skip` (their
        chunks could not be removed from Qdrant in this sweep) are left out. Reads only."""
        with self._lock:
            protected = self._protection(owner_id)
            batch, budget = RetentionBatch(owner_id), limit
            for run_id in run_ids:
                if budget <= 0:
                    break
                if self._has_operational_detail(owner_id, run_id):
                    batch.runs.append(run_id)
                for plan in self._observation_plans(owner_id, run_id, protected, skip):
                    if budget <= 0:
                        break
                    plan = ObservationPlan(plan.observation_id, plan.run_id, plan.document,
                                           plan.indices[:budget])
                    budget -= max(1, len(plan.indices))
                    batch.observations.append(plan)
                    if run_id not in batch.runs:
                        batch.runs.append(run_id)
            return batch

    def _has_operational_detail(self, owner_id: str, run_id: str) -> bool:
        kept = ",".join("?" * len(KEPT_EVENT_TYPES))
        return any(self._db.execute(sql, args).fetchone() for sql, args in (
            ("SELECT 1 FROM subtasks WHERE owner_id=? AND run_id=? LIMIT 1", (owner_id, run_id)),
            ("SELECT 1 FROM run_sources WHERE owner_id=? AND run_id=? LIMIT 1",
             (owner_id, run_id)),
            (f"SELECT 1 FROM run_events WHERE owner_id=? AND run_id=? "
             f"AND type NOT IN ({kept}) LIMIT 1", (owner_id, run_id, *KEPT_EVENT_TYPES)),
        ))

    def _observation_plans(self, owner_id: str, run_id: str, protected: ProtectedRefs,
                           skip: set[str]) -> list[ObservationPlan]:
        rows = self._db.execute(
            "SELECT o.id, s.kind FROM observations o LEFT JOIN sources s "
            "ON s.owner_id=o.owner_id AND s.id=o.source_id "
            "WHERE o.owner_id=? AND o.run_id=? ORDER BY o.fetched_at, o.id",
            (owner_id, run_id)).fetchall()
        plans = []
        for oid, kind in rows:
            if oid in skip:
                continue
            indices = self._removable(owner_id, oid, kind != "web", protected)
            if indices is None:
                continue
            plans.append(ObservationPlan(oid, run_id, kind != "web", tuple(indices)))
        return plans

    def _removable(self, owner_id: str, observation_id: str, document: bool,
                   protected: ProtectedRefs) -> list[int] | None:
        """The Passages of an Observation that may go, or None when nothing of it may (it is
        protected, or nothing is left to remove). A Source of unknown kind is treated as a
        document: its chunks are removed from Qdrant too, and only all together."""
        if document and observation_id in protected.observations:
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

    def apply(self, owner_id: str, batch: RetentionBatch, cutoff: datetime) -> Applied:
        """Delete what the batch planned that is, now, still eligible and unprotected, in one
        transaction. Raises MalformedRetentionRoot (nothing deleted) as `protection` does."""
        done = Applied()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            with self._db:
                runs = [r for r in batch.runs if self._still_expired(owner_id, r, cutoff)]
                eligible = set(runs)
                protected = self._protection(owner_id)
                touched_sources: set[str] = set()
                for plan in batch.observations:
                    if plan.run_id in eligible:
                        self._apply_observation(owner_id, plan, protected, touched_sources, done)
                for run_id in runs:
                    self._clean_run(owner_id, run_id, touched_sources, done)
                for source_id in sorted(touched_sources - protected.sources):
                    if self._source_unused(owner_id, source_id):
                        self._db.execute("DELETE FROM sources WHERE owner_id=? AND id=?",
                                         (owner_id, source_id))
                        done.sources += 1
        return done

    def _apply_observation(self, owner_id: str, plan: ObservationPlan,
                           protected: ProtectedRefs, touched_sources: set[str],
                           done: Applied) -> None:
        oid = plan.observation_id
        row = self._db.execute(
            "SELECT source_id, run_id FROM observations WHERE owner_id=? AND id=?",
            (owner_id, oid)).fetchone()
        if row is not None and row[1] != plan.run_id:
            return  # not the Run's Observation: never touched
        if plan.document and oid in protected.observations:
            return
        indices = [i for i in plan.indices if not protected.protects_passage(oid, i)]
        if indices:
            done.facts_detached += self._detach_facts(owner_id, oid, set(indices))
            done.passages += self._db.executemany(
                "DELETE FROM passages WHERE owner_id=? AND observation_id=? AND idx=?",
                [(owner_id, oid, i) for i in indices]).rowcount
        if row is None or oid in protected.observations:
            return
        if self._db.execute("SELECT 1 FROM passages WHERE owner_id=? AND observation_id=? "
                            "LIMIT 1", (owner_id, oid)).fetchone():
            return  # cited siblings remain: the Observation stays with them
        self._db.execute("DELETE FROM split_observations WHERE owner_id=? AND observation_id=?",
                         (owner_id, oid))
        done.observations += self._db.execute(
            "DELETE FROM observations WHERE owner_id=? AND id=?", (owner_id, oid)).rowcount
        touched_sources.add(row[0])

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
        touched_sources.update(s for (s,) in self._db.execute(
            "SELECT source_id FROM run_sources WHERE owner_id=? AND run_id=?",
            (owner_id, run_id)))
        done.run_links += self._db.execute(
            "DELETE FROM run_sources WHERE owner_id=? AND run_id=?", (owner_id, run_id)).rowcount
        done.subtasks += self._db.execute(
            "DELETE FROM subtasks WHERE owner_id=? AND run_id=?", (owner_id, run_id)).rowcount
        kept = ",".join("?" * len(KEPT_EVENT_TYPES))
        done.events += self._db.execute(
            f"DELETE FROM run_events WHERE owner_id=? AND run_id=? AND type NOT IN ({kept})",
            (owner_id, run_id, *KEPT_EVENT_TYPES)).rowcount

    def _source_unused(self, owner_id: str, source_id: str) -> bool:
        """No Observation (of any Run) and no Run link: a Run still fetching it links it in
        the same write that adds it (`SourceRepository.add`)."""
        return not any(self._db.execute(sql, (owner_id, source_id)).fetchone() for sql in (
            "SELECT 1 FROM observations WHERE owner_id=? AND source_id=? LIMIT 1",
            "SELECT 1 FROM run_sources WHERE owner_id=? AND source_id=? LIMIT 1",
        ))
