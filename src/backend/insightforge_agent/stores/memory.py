"""In-memory fakes of every repository. Used by default tests: no keys, no Docker."""

import threading

from insightforge_agent.stores.watchlist import MemoryWatchlistRepository
from datetime import datetime

from insightforge_agent.domain.contracts import ExtractedFact
from insightforge_agent.domain.errors import ClarificationConflict, NotFoundError
from insightforge_agent.domain.passages import check_passages
from insightforge_agent.domain.retention import FailureRecord
from insightforge_agent.domain.models import (
    TERMINAL_STATES,
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


class MemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], Run] = {}
        self._failures: dict[tuple[str, str], FailureRecord] = {}
        self._cas = threading.Lock()  # makes each compare-and-set below atomic, as in SQLite

    def add(self, run: Run) -> None:
        if (run.owner_id, run.id) in self._runs:
            raise ValueError(f"run {run.id} already exists")
        self._runs[(run.owner_id, run.id)] = run

    def get(self, owner_id: str, run_id: str) -> Run:
        try:
            return self._runs[(owner_id, run_id)]
        except KeyError:
            raise NotFoundError(f"run {run_id}") from None

    def list_for_owner(self, owner_id: str) -> list[Run]:
        own = [r for (owner, _), r in self._runs.items() if owner == owner_id]
        return sorted(own, key=lambda r: (r.created_at, r.id))

    def set_state(self, owner_id: str, run_id: str, state: RunState) -> Run:
        updated = self.get(owner_id, run_id).model_copy(update={"state": state})
        self._runs[(owner_id, run_id)] = updated
        return updated

    def fail(self, owner_id: str, run_id: str, *, reason: str, error_codes: list[str],
             failed_at: datetime) -> Run:
        with self._cas:
            run = self.get(owner_id, run_id)
            record = FailureRecord.at_failure(run, reason=reason, error_codes=error_codes,
                                              failed_at=failed_at)
            self._failures.setdefault((owner_id, run_id), record)
            updated = run.model_copy(update={"state": RunState.FAILED})
            self._runs[(owner_id, run_id)] = updated
            return updated

    def failure(self, owner_id: str, run_id: str) -> FailureRecord:
        try:
            return self._failures[(owner_id, run_id)]
        except KeyError:
            raise NotFoundError(f"failure record for run {run_id}") from None

    def _compare_and_set(self, owner_id: str, run_id: str, holds, update: dict,
                         conflict: str) -> Run:
        with self._cas:
            run = self.get(owner_id, run_id)
            if not holds(run):
                raise ClarificationConflict(
                    f"run {run_id} {conflict} (it is {run.state.value})")
            updated = run.model_copy(update=update)
            self._runs[(owner_id, run_id)] = updated
            return updated

    def open_clarification(self, owner_id: str, run_id: str,
                           clarification: Clarification) -> Run:
        return self._compare_and_set(
            owner_id, run_id,
            lambda r: r.state == RunState.PLANNING and r.clarification is None,
            {"state": RunState.AWAITING_CLARIFICATION, "clarification": clarification},
            "cannot ask a clarifying question")

    def resolve_clarification(self, owner_id: str, run_id: str, *, answer: str | None,
                              resolved_at: datetime) -> Run:
        with self._cas:
            run = self.get(owner_id, run_id)
            if run.state != RunState.AWAITING_CLARIFICATION:
                raise ClarificationConflict(
                    f"run {run_id} is not awaiting a clarification (it is {run.state.value})")
            resolved = run.clarification.model_copy(update={
                "resolution": "timed_out" if answer is None else "answered",
                "answer": answer, "resolved_at": resolved_at})
            updated = run.model_copy(
                update={"state": RunState.RESUMING, "clarification": resolved})
            self._runs[(owner_id, run_id)] = updated
            return updated

    def claim_resume(self, owner_id: str, run_id: str) -> Run:
        return self._compare_and_set(
            owner_id, run_id, lambda r: r.state == RunState.RESUMING,
            {"state": RunState.PLANNING}, "is not RESUMING")

    def list_unfinished(self) -> list[Run]:
        return sorted((r for r in self._runs.values() if r.state not in TERMINAL_STATES),
                      key=lambda r: (r.created_at, r.owner_id, r.id))


class MemorySubTaskRepository:
    """`event` goes to `events` before the change is made, so an append that fails leaves both
    unchanged (the SQLite repository uses one transaction)."""

    def __init__(self, events: "MemoryRunEventRepository | None" = None) -> None:
        self._plans: dict[tuple[str, str], list[SubTaskRecord]] = {}
        self._events = events

    def _log(self, event: RunEvent | None) -> None:
        if event is not None:
            if self._events is None:
                raise ValueError("this repository was built without a Run log")
            self._events.append(event)

    def save_plan(self, owner_id: str, run_id: str, records: list[SubTaskRecord],
                  event: RunEvent | None = None) -> None:
        self._log(event)
        self._plans[(owner_id, run_id)] = list(records)

    def list(self, owner_id: str, run_id: str) -> list[SubTaskRecord]:
        return list(self._plans.get((owner_id, run_id), []))

    def set_status(
        self, owner_id: str, run_id: str, subtask_id: str, status: SubTaskStatus,
        event: RunEvent | None = None,
    ) -> None:
        records = self._plans.get((owner_id, run_id), [])
        for i, rec in enumerate(records):
            if rec.subtask.id == subtask_id:
                self._log(event)
                records[i] = rec.model_copy(update={"status": status})
                return
        raise NotFoundError(f"sub-task {subtask_id}")


class MemorySourceRepository:
    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], Source] = {}
        self._run_links: dict[tuple[str, str], list[str]] = {}

    def add(self, source: Source) -> Source:
        stored = self._sources.setdefault((source.owner_id, source.id), source)
        linked = self._run_links.setdefault((source.owner_id, source.run_id), [])
        if source.id not in linked:
            linked.append(source.id)
        return stored

    def get(self, owner_id: str, source_id: str) -> Source:
        try:
            return self._sources[(owner_id, source_id)]
        except KeyError:
            raise NotFoundError(f"source {source_id}") from None

    def list_for_run(self, owner_id: str, run_id: str) -> list[Source]:
        ids = self._run_links.get((owner_id, run_id), [])
        return [self._sources[(owner_id, sid)] for sid in ids]


class MemoryObservationRepository:
    def __init__(self) -> None:
        self._observations: dict[tuple[str, str], Observation] = {}

    def add(self, observation: Observation) -> Observation:
        return self._observations.setdefault((observation.owner_id, observation.id), observation)

    def get(self, owner_id: str, observation_id: str) -> Observation:
        try:
            return self._observations[(owner_id, observation_id)]
        except KeyError:
            raise NotFoundError(f"observation {observation_id}") from None

    def list_for_source(self, owner_id: str, source_id: str) -> list[Observation]:
        found = [
            o for (owner, _), o in self._observations.items()
            if owner == owner_id and o.source_id == source_id
        ]
        return sorted(found, key=lambda o: o.fetched_at)


class MemoryPassageRepository:
    def __init__(self) -> None:
        self._passages: dict[tuple[str, str], dict[int, Passage]] = {}

    def add_all(self, owner_id: str, observation_id: str, passages: list[Passage]) -> None:
        check_passages(observation_id, passages)
        # A present key is the split marker, so an empty split counts as done.
        self._passages.setdefault((owner_id, observation_id), {p.index: p for p in passages})

    def list(self, owner_id: str, observation_id: str) -> list[Passage]:
        by_index = self._passages.get((owner_id, observation_id), {})
        return [by_index[i] for i in sorted(by_index)]

    def get(self, owner_id: str, observation_id: str, index: int) -> Passage:
        try:
            return self._passages[(owner_id, observation_id)][index]
        except KeyError:
            raise NotFoundError(f"passage {observation_id}#{index}") from None


class MemoryReportRepository:
    def __init__(self) -> None:
        self._reports: dict[tuple[str, str], Report] = {}

    def add(self, report: Report) -> None:
        if (report.owner_id, report.id) in self._reports:
            raise ValueError(f"report {report.id} already exists")
        if any(r.owner_id == report.owner_id and r.run_id == report.run_id
               for r in self._reports.values()):
            raise ValueError(f"run {report.run_id} already has a Report")
        self._reports[(report.owner_id, report.id)] = report

    def get(self, owner_id: str, report_id: str) -> Report:
        try:
            return self._reports[(owner_id, report_id)]
        except KeyError:
            raise NotFoundError(f"report {report_id}") from None

    def get_for_run(self, owner_id: str, run_id: str) -> Report:
        for (owner, _), report in self._reports.items():
            if owner == owner_id and report.run_id == run_id:
                return report
        raise NotFoundError(f"report for run {run_id}")

    def list_for_owner(self, owner_id: str) -> list[Report]:
        own = [r for (owner, _), r in self._reports.items() if owner == owner_id]
        return sorted(own, key=lambda r: (r.created_at, r.id))


class MemoryRunEventRepository:
    def __init__(self) -> None:
        self._events: list[RunEvent] = []

    def append(self, event: RunEvent) -> RunEvent:
        stored = event.model_copy(update={"seq": len(self._events) + 1})
        self._events.append(stored)
        return stored

    def read_after(
        self, owner_id: str, run_id: str, after_seq: int = 0, limit: int = 500
    ) -> list[RunEvent]:
        matching = [
            e for e in self._events
            if e.owner_id == owner_id and e.run_id == run_id and e.seq > after_seq
        ]
        return matching[:limit]


class MemoryRunSummaryRepository:
    def __init__(self) -> None:
        self._summaries: dict[tuple[str, str], RunSummary] = {}

    def add(self, summary: RunSummary) -> RunSummary:
        return self._summaries.setdefault((summary.owner_id, summary.run_id), summary)

    def get_for_run(self, owner_id: str, run_id: str) -> RunSummary:
        try:
            return self._summaries[(owner_id, run_id)]
        except KeyError:
            raise NotFoundError(f"run summary for run {run_id}") from None

    def list_for(self, owner_id: str, watchlist_item_id: str | None) -> list[RunSummary]:
        own = [s for (owner, _), s in self._summaries.items()
               if owner == owner_id and s.watchlist_item_id == watchlist_item_id]
        return sorted(own, key=lambda s: (s.created_at, s.run_id), reverse=True)


class MemoryEntityRepository:
    def __init__(self) -> None:
        self._facts: dict[tuple[str, str], dict[str, ExtractedFact]] = {}

    def upsert_facts(self, owner_id: str, run_id: str, facts: list[ExtractedFact]) -> None:
        stored = self._facts.setdefault((owner_id, run_id), {})
        for f in facts:
            stored[f.id] = f.model_copy(deep=True)

    def list_for_run(self, owner_id: str, run_id: str) -> list[ExtractedFact]:
        return [f.model_copy(deep=True) for f in self._facts.get((owner_id, run_id), {}).values()]


class MemoryRepos:
    """All repositories, wired the same way as `SqliteRepos`."""

    def __init__(self) -> None:
        self.runs = MemoryRunRepository()
        self.events = MemoryRunEventRepository()
        self.subtasks = MemorySubTaskRepository(self.events)
        self.sources = MemorySourceRepository()
        self.observations = MemoryObservationRepository()
        self.passages = MemoryPassageRepository()
        self.reports = MemoryReportRepository()
        self.entities = MemoryEntityRepository()
        self.summaries = MemoryRunSummaryRepository()
        self.lock = threading.RLock()  # same contract as SqliteRepos.lock
        self.watchlists = MemoryWatchlistRepository(self.runs, self.reports, self.lock)
