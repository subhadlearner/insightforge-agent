"""In-memory fakes of every repository. Used by default tests: no keys, no Docker."""

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


class MemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], Run] = {}

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


class MemorySubTaskRepository:
    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], list[SubTaskRecord]] = {}

    def save_plan(self, owner_id: str, run_id: str, records: list[SubTaskRecord]) -> None:
        self._plans[(owner_id, run_id)] = list(records)

    def list(self, owner_id: str, run_id: str) -> list[SubTaskRecord]:
        return list(self._plans.get((owner_id, run_id), []))

    def set_status(
        self, owner_id: str, run_id: str, subtask_id: str, status: SubTaskStatus
    ) -> None:
        records = self._plans.get((owner_id, run_id), [])
        for i, rec in enumerate(records):
            if rec.subtask.id == subtask_id:
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

    def add_all(self, owner_id: str, passages: list[Passage]) -> None:
        for observation_id in {p.observation_id for p in passages}:
            if (owner_id, observation_id) in self._passages:
                continue  # already split: never re-split
            indices = [p.index for p in passages if p.observation_id == observation_id]
            if len(indices) != len(set(indices)):
                raise ValueError(f"duplicate passage index in observation {observation_id}")
            self._passages[(owner_id, observation_id)] = {
                p.index: p for p in passages if p.observation_id == observation_id
            }

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


class MemoryRepos:
    """All repositories, wired the same way as `SqliteRepos`."""

    def __init__(self) -> None:
        self.runs = MemoryRunRepository()
        self.subtasks = MemorySubTaskRepository()
        self.sources = MemorySourceRepository()
        self.observations = MemoryObservationRepository()
        self.passages = MemoryPassageRepository()
        self.reports = MemoryReportRepository()
        self.events = MemoryRunEventRepository()
