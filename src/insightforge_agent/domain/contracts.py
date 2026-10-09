"""Stage contracts (design.md section 3): one model per transition, plain data, no I/O.

Nothing here holds a Source body. Passage text is reached only through `SourceStore`."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    model_config = ConfigDict(frozen=True)

    observation_id: str
    index: int


class ExtractedFact(BaseModel):
    """A structured fact one Passage states: who (`entity`), what (`predicate`), how much
    (`value`), for which `scope` and over which `period`. `statement` is the sentence."""

    id: str
    source_id: str
    passage: PassageRef
    statement: str
    entities: list[str] = Field(default_factory=list)
    entity: str = ""
    predicate: str = ""
    scope: str = ""
    value: str = ""
    period: str = "UNKNOWN"
    conflict: bool = False


class Conflict(BaseModel):
    """Facts about the same entity, predicate and scope that give different values for
    overlapping periods. Found over the structured facts, never over raw text."""

    entity: str
    predicate: str
    scope: str
    fact_ids: list[str]


class ExtractionResult(BaseModel):
    entities: list[str] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    id: str
    subtask_id: str
    aspect_id: str
    statement: str
    confidence: Confidence
    supporting: list[PassageRef]
    primary: PassageRef
    entity: str = ""
    predicate: str = ""
    scope: str = ""
    value: str = ""
    as_of_period: str = "UNKNOWN"
    derived_from: str | None = None  # "observation_date" when the period was derived
    temporally_ambiguous: bool = False
    conflicting: bool = False
    contradicting: list[PassageRef] = Field(default_factory=list)
    conflict_fact_ids: list[str] = Field(default_factory=list)
    relevance: float = 0.0  # cosine similarity between the item and the Brief


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


class VerifiedReport(BaseModel):
    """The draft plus Claim verdicts and the fact-check summary (design.md section 3).
    The T3 stage is a pass-through: no verdicts are produced yet."""

    draft: ReportDraft
    verdicts: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
