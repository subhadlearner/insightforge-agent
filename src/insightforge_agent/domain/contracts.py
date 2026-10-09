"""Stage contracts (design.md section 3): one model per transition, plain data, no I/O.

Nothing here holds a Source body. Passage text is reached only through `SourceStore`."""

from typing import Literal

from pydantic import BaseModel, Field

from insightforge_agent.domain.plan import SourceType

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
CONFIDENCE_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


class Brief(BaseModel):
    text: str = Field(min_length=1)
    source_toggles: dict[SourceType, bool] = Field(default_factory=lambda: {"web": True})
    document_ids: list[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    """All a Researcher ever sees of a Source."""

    source_id: str
    title: str
    summary: str


class SubTaskResult(BaseModel):
    subtask_id: str
    status: Literal["succeeded", "failed"]
    source_refs: list[SourceRef] = Field(default_factory=list)


class ResearchResults(BaseModel):
    results: list[SubTaskResult]


class PassageRef(BaseModel):
    observation_id: str
    index: int


class ExtractedFact(BaseModel):
    """A statement one Passage makes, with the entities it names."""

    source_id: str
    passage: PassageRef
    statement: str
    entities: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    entities: list[str] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # conflict detection is a later ticket


class EvidenceItem(BaseModel):
    id: str
    subtask_id: str
    aspect_id: str
    statement: str
    confidence: Confidence
    supporting: list[PassageRef]
    primary: PassageRef
    as_of_period: str = "UNKNOWN"


class EvidenceSection(BaseModel):
    aspect_id: str
    heading: str
    confidence: Confidence
    items: list[EvidenceItem]


class EvidenceBundle(BaseModel):
    sections: list[EvidenceSection]


class Claim(BaseModel):
    text: str
    evidence_id: str
    historical: bool = False


class ReportSection(BaseModel):
    heading: str
    claims: list[Claim]


class ReportDraft(BaseModel):
    title: str
    summary: str = ""
    sections: list[ReportSection]
    gaps: list[str] = Field(default_factory=list)
