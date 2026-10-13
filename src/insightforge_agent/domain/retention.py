"""Retention rules (design.md section 6, Retention): what a sweep may remove, and what survives.

Pure rules, no I/O. Eligibility is by reference, never by a Run's status alone:

- A terminal Run's raw material (its Observations and their Passages) becomes a candidate once
  its terminal timestamp is at or before the cutoff (`now - retention_days`). A COMPLETE Run's
  timestamp is its Report's `created_at`; a FAILED Run's is its failure record's
  `retention_starts_at`. A non-terminal Run's material is never a candidate.
- A candidate survives while any retained record refers to it: a Report (Evidence items,
  Historical items, fact-check verdicts), a Watchlist Baseline seed, an established Finding,
  the Baseline-normalisation detail or the Diff log. `ProtectedRefs` collects those references.
- A retained record that cannot be interpreted is never permission to delete: the collector
  raises `MalformedRetentionRoot`, and the sweep then removes nothing of that User."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from insightforge_agent.domain.alerts import Alert
from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    HistoricalContext,
    ReportDraft,
    VerifiedReport,
)
from insightforge_agent.domain.models import Run
from insightforge_agent.domain.diff import DiffEntry, EstablishedFinding
from insightforge_agent.domain.findings import Skipped
from insightforge_agent.domain.watchlist import Baseline

DEFAULT_RETENTION_DAYS = 30

# Stable error codes on a failure record. One per way a Run can fail, plus the one added when a
# stored Report could not be finalised; the human-readable reason carries the detail.
PIPELINE_FAILURE = "pipeline_failure"  # a stage raised RunFailed (limits, plan bounds, Writer)
UNEXPECTED_ERROR = "unexpected_error"  # any other exception from the graph
PROCESS_INTERRUPTED = "process_interrupted"  # the startup sweep found it in progress
NOT_STARTED = "not_started"  # cancelled by shutdown before it ran
CHECKPOINT_MISSING = "checkpoint_missing"  # a resume found no question in its checkpoint
CLARIFICATION_CONFLICT = "clarification_conflict"  # it could not wait for its answer
REPORT_NOT_FINALISED = "report_not_finalised"  # its stored Report could not be finalised
LEGACY_UNRECORDED = "legacy_unrecorded"  # FAILED before failure records were kept


class FailureRecord(BaseModel):
    """The compact record a Failed Run keeps after its raw material expires. Written in the
    same transaction that makes the Run FAILED, and never removed by retention.

    `retention_starts_at` is what the sweep's cutoff is compared with. For a failure recorded
    when it happened it equals `failed_at`. For a Run that failed before records were kept
    (`backfilled`), the failure time is unknown (`failed_at` is None, never guessed from the
    Run's creation) and retention starts when the record was backfilled."""

    model_config = ConfigDict(frozen=True)

    owner_id: str
    run_id: str
    run_created_at: datetime
    failed_at: datetime | None
    retention_starts_at: datetime
    stage: str | None  # the Run state it failed in; None when unknown (backfilled)
    reason: str
    error_codes: list[str] = Field(min_length=1)
    backfilled: bool = False

    @classmethod
    def at_failure(cls, run: Run, *, reason: str, error_codes: list[str],
                   failed_at: datetime) -> "FailureRecord":
        """The record of a Run failing now, in the state it is in."""
        return cls(owner_id=run.owner_id, run_id=run.id, run_created_at=run.created_at,
                   failed_at=failed_at, retention_starts_at=failed_at, stage=run.state.value,
                   reason=failure_reason(reason), error_codes=error_codes)

    @classmethod
    def legacy(cls, run: Run, *, reason: str, backfilled_at: datetime) -> "FailureRecord":
        """A Run that failed before records were kept: its failure time and stage are
        unknown, and retention starts at backfill time, never earlier."""
        return cls(owner_id=run.owner_id, run_id=run.id, run_created_at=run.created_at,
                   failed_at=None, retention_starts_at=backfilled_at, stage=None,
                   reason=failure_reason(reason), error_codes=[LEGACY_UNRECORDED],
                   backfilled=True)


MAX_REASON_CHARS = 1000


def failure_reason(reason: str) -> str:
    return reason if len(reason) <= MAX_REASON_CHARS else reason[:MAX_REASON_CHARS - 1] + "…"


def retention_cutoff(now: datetime, retention_days: int) -> datetime:
    if retention_days < 1:
        raise ValueError("retention_days must be a positive number of days")
    return now - timedelta(days=retention_days)


def expired(terminal_at: datetime, cutoff: datetime) -> bool:
    """A terminal timestamp exactly at the cutoff is expired."""
    return terminal_at <= cutoff


class MalformedRetentionRoot(ValueError):
    """A retained record whose references cannot be read safely. Nothing of its User may be
    removed until it is repaired."""


@dataclass
class ProtectedRefs:
    """Everything one User's retained records refer to. A Passage in `passages` keeps its row,
    and its Observation (in `observations`) keeps its row and its Source, as does an
    Observation named for its date (`observation_dates`). An Observation referred to as a whole
    (a reference with no Passage index) is in `whole`: it keeps every Passage. A Source in `sources` keeps its
    row. An uploaded document's Observation with any protected Passage is kept whole by the
    store, because a document Claim is fact-checked against all of its chunks."""

    passages: set[tuple[str, int]] = field(default_factory=set)
    observations: set[str] = field(default_factory=set)
    whole: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)

    def add_whole(self, observation_id: str) -> None:
        self.whole.add(observation_id)
        self.observations.add(observation_id)

    def add_passage(self, observation_id: str, index: int) -> None:
        self.passages.add((observation_id, index))
        self.observations.add(observation_id)

    def protects_passage(self, observation_id: str, index: int) -> bool:
        return (observation_id, index) in self.passages or observation_id in self.whole

    # --- the roots ---------------------------------------------------------------------------

    def add_report(self, body: Any, what: str) -> None:
        """A persisted Report body: Evidence items (primary, supporting, contradicting),
        Historical items (primary, supporting) and verdicts (original, matched)."""
        if not isinstance(body, dict):
            raise MalformedRetentionRoot(f"{what}: the body is not a mapping")
        _validate(what, (
            (EvidenceBundle, body.get("evidence")),
            (HistoricalContext, body.get("historical")),
            (VerifiedReport, body.get("verified")),
            (ReportDraft, body.get("draft")),
        ))
        self._walk(body)

    def add_baseline_seed(self, baseline: Any, what: str) -> None:
        """A T11 Baseline seed: each seed's Evidence item, Observations and Sources."""
        _validate(what, ((Baseline, baseline),))
        self._walk(baseline)

    def add_established(self, state: Any, what: str) -> None:
        """One established Finding's provenance."""
        _validate(what, ((EstablishedFinding, state),))
        self._walk(state)

    def add_diff_detail(self, detail: Any, what: str) -> None:
        """A Diff log record or the Baseline-normalisation detail. Each entry may hold this
        Run's Finding and the earlier established one; both keep their provenance."""
        if not isinstance(detail, dict):
            raise MalformedRetentionRoot(f"{what}: the detail is not a mapping")
        entries, skipped = detail.get("entries", []), detail.get("skipped", [])
        if not isinstance(entries, list) or not isinstance(skipped, list):
            raise MalformedRetentionRoot(f"{what}: entries or skipped is not a list")
        _validate(what, [(DiffEntry, e) for e in entries] + [(Skipped, s) for s in skipped])
        self._walk(detail)

    def add_alert(self, alert: Any, what: str) -> None:
        _validate(what, ((Alert, alert),))
        self._walk(alert)

    def _walk(self, value: Any) -> None:
        """Every reference anywhere in a record, typed fields or not, so a field added later
        is protected before anyone teaches this module about it: any mapping with an
        `observation_id` (a Passage reference when it has an `index`, else the whole
        Observation), the keys of `observation_dates` (Observation rows), and every
        `source_id` / `source_ids`. An `index` that is not a non-negative integer is malformed."""
        stack = [value]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            oid = node.get("observation_id")
            if oid is not None:
                if not isinstance(oid, str):
                    raise MalformedRetentionRoot(f"observation_id {oid!r} is not a string")
                if "index" not in node:
                    self.add_whole(oid)
                else:
                    index = node["index"]
                    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                        raise MalformedRetentionRoot(f"Passage index {index!r} of {oid}")
                    self.add_passage(oid, index)
            for key, child in node.items():
                if key == "observation_dates":
                    if not isinstance(child, dict):
                        raise MalformedRetentionRoot("observation_dates is not a mapping")
                    # Named for its fetch date: the Observation row is what must resolve.
                    self.observations.update(str(k) for k in child)
                elif key == "source_ids":
                    if not isinstance(child, list):
                        raise MalformedRetentionRoot("source_ids is not a list")
                    self.sources.update(str(s) for s in child)
                elif key == "source_id" and isinstance(child, str):
                    self.sources.add(child)
                elif isinstance(child, dict | list):
                    stack.append(child)


def _validate(what: str, checks: Iterable[tuple[type[BaseModel], Any]]) -> None:
    for model, value in checks:
        if value is None and model in (EvidenceBundle, HistoricalContext, VerifiedReport,
                                       ReportDraft):
            continue  # a Report stored before that part existed
        try:
            model.model_validate(value)
        except ValidationError as error:
            raise MalformedRetentionRoot(
                f"{what}: not a valid {model.__name__}: {error.error_count()} error(s)"
            ) from error

