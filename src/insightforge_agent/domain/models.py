"""Persisted records (design.md section 6). Plain data, no I/O."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class Run(_Record):
    id: str
    owner_id: str
    brief: str
    state: RunState
    created_at: datetime
    watchlist_item_id: str | None = None


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


class RunEvent(_Record):
    """One append-only progress entry. `seq` is assigned on append and is the cursor."""

    seq: int = 0
    owner_id: str
    run_id: str
    type: str
    payload: dict[str, Any]
    created_at: datetime
