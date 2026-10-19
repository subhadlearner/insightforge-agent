"""Repository interfaces. Every read and write is scoped by the owning User.

A record that does not exist, or belongs to someone else, is a NotFoundError."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from insightforge_agent.domain.watchlist import Baseline, WatchlistItem
from insightforge_agent.domain.contracts import ExtractedFact
from insightforge_agent.domain.retention import FailureRecord
from insightforge_agent.domain.models import (
    Clarification,
    Observation,
    Passage,
    Report,
    Run,
    RunEvent,
    RunState,
    RunSummary,
    Source,
    SubTaskRecord,
    SubTaskStatus,
)


@runtime_checkable
class RunRepository(Protocol):
    def add(self, run: Run) -> None:
        """Raises ValueError if the User already has a Run with this id."""
        ...
    def get(self, owner_id: str, run_id: str) -> Run: ...
    def list_for_owner(self, owner_id: str) -> list[Run]:
        """Oldest first."""
        ...
    def set_state(self, owner_id: str, run_id: str, state: RunState) -> Run: ...

    def fail(self, owner_id: str, run_id: str, *, reason: str, error_codes: list[str],
             failed_at: datetime) -> Run:
        """Run -> FAILED together with its failure record (one atomic write). The record's
        stage is the state the Run was in; a Run that already has a record keeps it."""
        ...

    def failure(self, owner_id: str, run_id: str) -> FailureRecord:
        """The Failed Run's compact record, which retention never removes."""
        ...

    # Clarification (design.md section 2). Each is one atomic compare-and-set on the Run row;
    # a caller that loses gets ClarificationConflict and changes nothing.
    def open_clarification(self, owner_id: str, run_id: str,
                           clarification: Clarification) -> Run:
        """PLANNING -> AWAITING_CLARIFICATION with the question stored, only if the Run has
        never asked one."""
        ...

    def resolve_clarification(self, owner_id: str, run_id: str, *, answer: str | None,
                              resolved_at: datetime) -> Run:
        """AWAITING_CLARIFICATION -> RESUMING, storing the resolution in the same write: the
        answer, or a timeout when `answer` is None. The first resolver wins."""
        ...

    def claim_resume(self, owner_id: str, run_id: str) -> Run:
        """RESUMING -> PLANNING: exactly one caller may carry a resolved Run on."""
        ...

    def list_unfinished(self) -> list[Run]:
        """Every User's Runs that are not COMPLETE or FAILED, oldest first. For the startup
        sweep only, which acts for the system and not for one User."""
        ...


@runtime_checkable
class SubTaskRepository(Protocol):
    """`event`, when given, is appended to the Run log in the same atomic write as the change:
    both are stored or neither is, so the Run page (which reads the log) and the stored plan
    or outcome never disagree."""

    def save_plan(self, owner_id: str, run_id: str, records: list[SubTaskRecord],
                  event: RunEvent | None = None) -> None: ...
    def list(self, owner_id: str, run_id: str) -> list[SubTaskRecord]: ...
    def set_status(
        self, owner_id: str, run_id: str, subtask_id: str, status: SubTaskStatus,
        event: RunEvent | None = None,
    ) -> None:
        """Raises NotFoundError for an unknown Sub-task; nothing is stored then."""
        ...


@runtime_checkable
class SourceRepository(Protocol):
    def add(self, source: Source) -> Source:
        """Idempotent: an existing id is returned unchanged, never overwritten. Either way
        the Source is linked to `source.run_id`, so every Run that fetched it can list it."""
        ...

    def get(self, owner_id: str, source_id: str) -> Source: ...
    def list_for_run(self, owner_id: str, run_id: str) -> list[Source]: ...


@runtime_checkable
class ObservationRepository(Protocol):
    def add(self, observation: Observation) -> Observation:
        """Idempotent: an existing id is returned unchanged."""
        ...

    def get(self, owner_id: str, observation_id: str) -> Observation: ...
    def list_for_source(self, owner_id: str, source_id: str) -> list[Observation]:
        """Oldest first."""
        ...


@runtime_checkable
class PassageRepository(Protocol):
    def add_all(self, owner_id: str, observation_id: str, passages: list[Passage]) -> None:
        """Stores an Observation's Passages once, and records that the split is done, even
        when it produced no Passages. All of it is one atomic write. An Observation already
        split is left untouched, so it is never re-split. Raises ValueError if a Passage
        belongs to another Observation or an index is repeated; nothing is stored then."""
        ...

    def list(self, owner_id: str, observation_id: str) -> list[Passage]:
        """In index order."""
        ...

    def get(self, owner_id: str, observation_id: str, index: int) -> Passage: ...


@runtime_checkable
class ReportRepository(Protocol):
    def add(self, report: Report) -> None:
        """One Report per Run. Raises ValueError if the User's Run already has one."""
        ...
    def get(self, owner_id: str, report_id: str) -> Report: ...
    def get_for_run(self, owner_id: str, run_id: str) -> Report: ...
    def list_for_owner(self, owner_id: str) -> list[Report]:
        """Oldest first."""
        ...


@runtime_checkable
class RunSummaryRepository(Protocol):
    def add(self, summary: RunSummary) -> RunSummary:
        """Idempotent per Run: the first summary stored for a Run is kept and returned."""
        ...

    def get_for_run(self, owner_id: str, run_id: str) -> RunSummary: ...
    def list_for(self, owner_id: str, watchlist_item_id: str | None) -> list[RunSummary]:
        """The User's summaries for one Watchlist Item, or for one-shot Runs when
        `watchlist_item_id` is None. Newest first."""
        ...


@runtime_checkable
class RunEventRepository(Protocol):
    def append(self, event: RunEvent) -> RunEvent:
        """Assigns `seq`, which increases per append. Events are never changed. Retention
        removes an expired Run's detailed events, keeping its stage and failure events."""
        ...

    def read_after(
        self, owner_id: str, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[RunEvent]:
        """Events with seq greater than `after_seq`, in order: the SSE cursor."""
        ...


@runtime_checkable
class EntityRepository(Protocol):
    """Extracted entities and structured facts, with their Passage references."""

    def upsert_facts(self, owner_id: str, run_id: str, facts: list[ExtractedFact]) -> None:
        """Idempotent by fact id: a fact stored again replaces the earlier copy in place
        (conflict detection re-stores facts with `conflict` set)."""
        ...

    def list_for_run(self, owner_id: str, run_id: str) -> list[ExtractedFact]:
        """In the order first stored."""
        ...


@runtime_checkable
class WatchlistRepository(Protocol):
    """Configuration and atomic admission; Run rows are the durable max_runs counter.
    System-only list_all is for startup reconciliation, never an API owner lookup.
    """
    def add(self, item: "WatchlistItem") -> None: ...
    def get(self, owner_id: str, item_id: str) -> "WatchlistItem": ...
    def list_for_owner(self, owner_id: str) -> list["WatchlistItem"]: ...
    def list_all(self) -> list["WatchlistItem"]: ...
    def update(
        self,
        item: "WatchlistItem",
        expected_revision: int,
        now: datetime | Callable[[], datetime],
    ) -> "WatchlistItem": ...
    def delete(self, owner_id: str, item_id: str) -> None: ...
    def counts(self, owner_id: str, item_id: str) -> tuple[int, int, int]: ...
    def recent_failed_runs(
        self, owner_id: str, item_id: str, limit: int = 5
    ) -> list[Run]:
        """This item's newest Failed Runs, newest first, without scanning the owner's Runs."""
        ...
    def start(
        self,
        run: Run,
        expected_revision: int,
        admission_open: Callable[[], bool] | None = None,
    ) -> None:
        """Store a Run only when owned, current, below max_runs and without an active Run."""
        ...
    def take_tick(self, owner_id: str, item_id: str, revision: int,
                  now: datetime) -> "WatchlistItem | None": ...
    def skip_missed(self, owner_id: str, item_id: str, now: datetime) -> "WatchlistItem": ...
    def baseline_candidates(
        self, owner_id: str, item_id: str, schema_version: int
    ) -> list[Report]:
        """Usable COMPLETE Reports for this item, oldest first."""
        ...
    def reject_baseline_candidate(
        self,
        owner_id: str,
        item_id: str,
        run_id: str,
        reason: str,
        failed_at: datetime,
        schema_version: int,
    ) -> bool:
        """Persist a corrupt candidate once; return whether this call first recorded it."""
        ...
    def baseline(self, owner_id: str, item_id: str) -> "Baseline | None": ...
    def baseline_state(
        self, owner_id: str, item_id: str
    ) -> Literal["none", "seed", "established"]:
        """`none`, `seed` (T11 input awaiting normalisation) or `established` (T12)."""
        ...
    def initialize_baseline(self, baseline: "Baseline") -> "Baseline": ...
