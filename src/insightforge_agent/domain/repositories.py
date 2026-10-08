"""Repository interfaces. Every read and write is scoped by the owning User.

A record that does not exist, or belongs to someone else, is a NotFoundError."""

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class SubTaskRepository(Protocol):
    def save_plan(self, owner_id: str, run_id: str, records: list[SubTaskRecord]) -> None: ...
    def list(self, owner_id: str, run_id: str) -> list[SubTaskRecord]: ...
    def set_status(
        self, owner_id: str, run_id: str, subtask_id: str, status: SubTaskStatus
    ) -> None: ...


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
class RunEventRepository(Protocol):
    def append(self, event: RunEvent) -> RunEvent:
        """Assigns `seq`, which increases per append. Events are never changed or removed."""
        ...

    def read_after(
        self, owner_id: str, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[RunEvent]:
        """Events with seq greater than `after_seq`, in order: the SSE cursor."""
        ...
