"""Persisted records (design.md section 6). Plain data, no I/O."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from insightforge_agent.domain.evidence import domain_of
from insightforge_agent.domain.plan import SubTask


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class User(_Record):
    """A persona from users.yml. Carries no keys."""

    id: str
    display_name: str
    source_toggles: dict[str, bool] = Field(default_factory=dict)
    webhook_url: str | None = None


class RunState(StrEnum):
    INGESTING = "INGESTING"
    PLANNING = "PLANNING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    RESUMING = "RESUMING"
    RESEARCHING = "RESEARCHING"
    EXTRACTING = "EXTRACTING"
    SYNTHESIZING = "SYNTHESIZING"
    WRITING = "WRITING"
    FACT_CHECKING = "FACT_CHECKING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset({RunState.COMPLETE, RunState.FAILED})


class Clarification(_Record):
    """The one clarifying question of a Run (design.md section 2, Clarification). Stored on the
    Run itself, so the compare-and-set that resolves it also moves the Run out of
    AWAITING_CLARIFICATION: a Run is never RESUMING without its resolution."""

    question: str
    asked_at: datetime  # when the Run entered AWAITING_CLARIFICATION
    deadline: datetime  # asked_at plus the clarification timeout
    resolution: Literal["answered", "timed_out"] | None = None
    answer: str | None = None
    resolved_at: datetime | None = None

    def expired(self, now: datetime) -> bool:
        """The deadline has passed: only the timeout may resolve the question now."""
        return now >= self.deadline


class Run(_Record):
    id: str
    owner_id: str
    brief: str
    state: RunState
    created_at: datetime
    watchlist_item_id: str | None = None
    clarification: Clarification | None = None  # at most one per Run


class SubTaskStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SubTaskRecord(_Record):
    run_id: str
    subtask: SubTask
    status: SubTaskStatus = SubTaskStatus.PENDING


SourceKind = Literal["web", "pdf"]


class Source(_Record):
    """Raw fetched material, stored by reference. Its id is derived from its locator."""

    id: str
    owner_id: str
    run_id: str
    kind: SourceKind
    locator: str
    title: str
    credibility_score: float | None = Field(default=None, ge=0, le=1)
    published_at: datetime | None = None

    @property
    def domain(self) -> str | None:
        """A web Source's host name without "www.", as an inline citation shows it; None for
        an uploaded document, which has no domain."""
        if self.kind != "web" or not urlsplit(self.locator).hostname:
            return None  # never the raw locator, which `domain_of` falls back to
        return domain_of(self.locator)


class Observation(_Record):
    """One fetch of a Source at a point in time."""

    id: str
    owner_id: str
    source_id: str
    run_id: str
    fetched_at: datetime


class Passage(_Record):
    """One addressable paragraph of an Observation, fixed when first split."""

    observation_id: str
    index: int = Field(ge=0)
    text: str
    page: int | None = None
    section_heading: str | None = None


class Report(_Record):
    id: str
    owner_id: str
    run_id: str
    created_at: datetime
    body: dict[str, Any]


class RunSummary(_Record):
    """A compressed record of one Run, written once before it becomes COMPLETE, derived from its
    stored records without a model call (design.md section 4, Memory). The Planner reads it;
    it never holds a Report body, a Planner message or a transcript."""

    owner_id: str
    run_id: str
    watchlist_item_id: str | None = None
    created_at: datetime
    brief: str
    themes: list[str] = Field(default_factory=list)  # the Report's section headings (Aspects)
    key_entities: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    # At least one of the Report's Claims is in `report_sections`, so memory can search it.
    memory_indexed: bool = False


class RunEvent(_Record):
    """One append-only progress entry. `seq` is assigned on append and is the cursor."""

    seq: int = 0
    owner_id: str
    run_id: str
    type: str
    payload: dict[str, Any]
    created_at: datetime
