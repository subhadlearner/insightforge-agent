"""Stage contracts (design.md section 3): one model per transition, plain data, no I/O.

Nothing here holds a Source body. Passage text is reached only through `SourceStore`."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from insightforge_agent.domain.plan import SourceType

Confidence = Literal["HIGH", "MEDIUM", "LOW"]
CONFIDENCE_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


WEB_ONLY: dict[str, bool] = {"web": True}


def effective_source_toggles(explicit: dict | None, persona: dict[str, bool]) -> dict:
    """The source toggles a Brief uses: the explicit ones when given (even empty), else the
    persona's defaults from users.yml, else web only. Run submission and the runtime estimate
    both use this, so they never disagree about what an omitted toggle means."""
    if explicit is not None:
        return explicit
    return dict(persona) if persona else dict(WEB_ONLY)


class Brief(BaseModel):
    text: str = Field(min_length=1)
    source_toggles: dict[SourceType, bool] = Field(default_factory=lambda: dict(WEB_ONLY))
    document_ids: list[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    """All a Researcher ever sees of a Source."""

    source_id: str
    title: str
    summary: str
    # The chunks a Doc Reader retrieved (references only, never text). Empty means the whole
    # Source, as for a fetched web page.
    passages: list["PassageRef"] = Field(default_factory=list)


class SubTaskResult(BaseModel):
    subtask_id: str
    status: Literal["succeeded", "failed"]
    source_refs: list[SourceRef] = Field(default_factory=list)
    # T18 CS2: "search_recovery" when a failed Document Reader's own search results were
    # recovered as the scope (the Sub-task itself stays FAILED for the user).
    scope_origin: Literal["doc_reader", "search_recovery"] = "doc_reader"

    def passage_scope(self) -> "PassageScope":
        """Per Source, the exact Passages this Sub-task retrieved (by Observation and index),
        or None when it reads the whole Source, as for a web page."""
        return merge_scopes([{r.source_id: set(r.passages) or None} for r in self.source_refs])


# Source ID -> the Passages to read, or None for the Source's latest Observation, whole.
PassageScope = dict[str, "set[PassageRef] | None"]


def merge_scopes(scopes) -> PassageScope:
    """Union of Passage scopes. A Source any scope reads whole is read whole."""
    out: PassageScope = {}
    for scope in scopes:
        for source_id, refs in scope.items():
            if refs is None or (source_id in out and out[source_id] is None):
                out[source_id] = None
            else:
                out.setdefault(source_id, set()).update(refs)
    return out


class ResearchResults(BaseModel):
    results: list[SubTaskResult]

    def passage_scope(self) -> "PassageScope":
        """Per Source, the Passages any Sub-task retrieved, or None for the whole Source."""
        return merge_scopes(r.passage_scope() for r in self.results)


class PassageRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: str
    index: int


SourceRef.model_rebuild()
SubTaskResult.model_rebuild()
ResearchResults.model_rebuild()


class ExtractedFact(BaseModel):
    """A structured fact one Passage states: who (`entity`), what (`predicate`), how much
    (`value`), for which `scope` and over which `period`. `statement` is the sentence.

    Extraction always records its Passage and Source (`provenance="available"`). The fact
    outlives that Passage: when retention removes it, the fact is kept with
    `provenance="unavailable"` and no `source_id` or `passage`, never a dangling reference."""

    id: str
    source_id: str | None
    passage: PassageRef | None
    provenance: Literal["available", "unavailable"] = "available"
    statement: str
    entities: list[str] = Field(default_factory=list)
    entity: str = ""
    predicate: str = ""
    scope: str = ""
    value: str = ""
    period: str = "UNKNOWN"
    conflict: bool = False
    # ADR-0006: the Sub-task the fact serves and the research requirements it helps answer, as
    # Extraction proposed them and code accepted them (known ids only). Empty for older facts.
    subtask_id: str = ""
    requirement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _provenance_matches_references(self) -> "ExtractedFact":
        resolvable = self.source_id is not None and self.passage is not None
        if resolvable != (self.provenance == "available"):
            raise ValueError("an available provenance needs its Source and Passage, and an "
                             "unavailable one must not name them")
        return self

    def without_provenance(self) -> "ExtractedFact":
        return self.model_copy(update={"source_id": None, "passage": None,
                                       "provenance": "unavailable"})


class Conflict(BaseModel):
    """Facts about the same entity, predicate and scope that give different values for
    overlapping periods. Found over the structured facts, never over raw text."""

    entity: str
    predicate: str
    scope: str
    fact_ids: list[str]


class FailedUnit(BaseModel):
    """A model work unit whose output could not be used after bounded recovery (ADR-0006 §4):
    what it covered, so the loss stays visible instead of reading as "no facts"."""

    stage: str  # "extraction"
    source_id: str
    passages: list[PassageRef]
    reason: str  # "truncated", "invalid" or "rejected"


class ExtractionResult(BaseModel):
    entities: list[str] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    failed_units: list[FailedUnit] = Field(default_factory=list)


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
    # T18 CS4: the primary Source's publication date, and whether an item with no stated period
    # was published before the time window its Sub-task asked for (possibly stale, not wrong).
    published_at: date | None = None
    before_window: bool = False
    conflicting: bool = False
    contradicting: list[PassageRef] = Field(default_factory=list)
    conflict_fact_ids: list[str] = Field(default_factory=list)
    relevance: float = 0.0  # cosine similarity between the item and the Brief
    requirement_ids: list[str] = Field(default_factory=list)  # ADR-0006 ledger links


class EvidenceSection(BaseModel):
    aspect_id: str
    heading: str
    confidence: Confidence
    items: list[EvidenceItem]


class EvidenceBundle(BaseModel):
    sections: list[EvidenceSection]


class HistoricalItem(BaseModel):
    """Background from one Claim of an earlier Report of the same User (design.md section 5,
    Memory researcher). Deliberately not an Evidence item: it never enters the Evidence bundle,
    has no Confidence and can never establish or confirm a Finding. Its lineage is copied from
    the persisted Report, never from a model or the search index, and is flat: a Claim that was
    itself historical passes on its own original Report and Passages, never a chain."""

    id: str
    subtask_id: str
    aspect_id: str
    heading: str  # the Aspect name, where the Writer places the Claim
    statement: str  # the earlier Claim, as that Report's Writer wrote it
    entity: str = ""
    report_id: str  # the Report that first made the Claim
    run_id: str
    report_date: date  # the original date shown inline; not the As-of period
    as_of_period: str = "UNKNOWN"  # the period the original Evidence item covered
    primary: PassageRef  # the original Evidence item's primary Passage
    supporting: list[PassageRef] = Field(default_factory=list)


class HistoricalContext(BaseModel):
    """What the Memory researcher found, carried beside the Evidence bundle, never inside it."""

    items: list[HistoricalItem] = Field(default_factory=list)

    def by_id(self) -> dict[str, HistoricalItem]:
        return {i.id: i for i in self.items}


class Claim(BaseModel):
    text: str
    evidence_id: str  # an Evidence item, or a HistoricalItem when `historical`
    historical: bool = False
    # ADR-0006: further current Evidence items an analytical Claim (a comparison or conclusion)
    # rests on. `evidence_id` stays its primary citation; empty for a single-item Claim.
    premises: list[str] = Field(default_factory=list)

    def evidence_ids(self) -> list[str]:
        """The primary item and every premise, in order, without repeats."""
        return list(dict.fromkeys([self.evidence_id, *self.premises]))


class ReportSection(BaseModel):
    heading: str
    claims: list[Claim]


class ReportDraft(BaseModel):
    title: str
    summary: str = ""
    sections: list[ReportSection]
    gaps: list[str] = Field(default_factory=list)
    # ADR-0006: what code had to do to make the draft publishable (a Claim replaced by its
    # item's statement, a section or requirement filled verbatim, an analytical Claim removed).
    # A flagged Report is degraded output, never evidence of the Writer's synthesis.
    quality: list[str] = Field(default_factory=list)


# A section's label: its Confidence, or BACKGROUND for Historical background only.
SectionLabel = Literal["HIGH", "MEDIUM", "LOW", "BACKGROUND"]


def section_confidence(draft: ReportDraft, bundle: EvidenceBundle,
                       historical: HistoricalContext | None = None) -> list[SectionLabel | None]:
    """Each Report section's label, derived when the Report is read from the Evidence bundle
    and Historical context stored with it, so Reports written before it existed get labels too.

    A section whose current Claims all cite bundle items takes the Confidence of the Evidence
    sections they cite; when they cite several (only possible in a Report written before Claims
    were filed by section), the lowest. A section of Historical Claims only, each citing a
    Historical item, is BACKGROUND: it has no current Confidence. Anything else (a Claim citing
    an id the stored records do not hold, or no Claims at all) is None, unresolved, never
    shown as background and never given an invented label."""
    of_item = {i.id: s.confidence for s in bundle.sections for i in s.items}
    background = (historical or HistoricalContext()).by_id()
    out: list[SectionLabel | None] = []
    for section in draft.sections:
        current = [c for c in section.claims if not c.historical]
        past = [c for c in section.claims if c.historical]
        if (not section.claims or any(c.evidence_id not in of_item for c in current)
                or any(c.evidence_id not in background for c in past)):
            out.append(None)
        elif current:
            out.append(min((of_item[c.evidence_id] for c in current),
                           key=CONFIDENCE_ORDER.__getitem__))
        else:
            out.append("BACKGROUND")
    return out


class ClaimVerdict(BaseModel):
    """The Fact-Checker's verdict on one sampled Claim, named by its position in the draft.
    `matched` is the new Passage that best matches the Claim; `original` is the Passage the
    Claim cites, which a check never changes."""

    section: int
    claim: int
    evidence_id: str
    verdict: Literal["verified", "unverified", "unchecked"]
    # A Historical Claim is checked against its original Sources: the `historical_` kinds.
    check: Literal["web_refetch", "document_passages", "historical_web_refetch",
                   "historical_document_passages", "premises_joint", "not_checkable"]
    original: PassageRef
    similarity: float | None = None
    matched: PassageRef | None = None
    reason: str = ""
    # ADR-0006: the support verdict of the cited Passage as it was when cited (citation truth),
    # kept apart from the verdict on the re-fetched page (current truth). None for older Reports.
    cited_support: str | None = None


class VerifiedReport(BaseModel):
    """The draft plus Claim verdicts and the fact-check summary (design.md sections 3 and 7).
    An `unverified` Claim's text in `draft` ends with `UNVERIFIED_MARK`."""

    draft: ReportDraft
    verdicts: list[ClaimVerdict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class Citation(BaseModel):
    """What the citation drawer shows for one Claim: its primary Passage and that Passage's
    Source. Resolved from stored records, so it exists for every Claim, sampled or not."""

    section: int
    claim: int
    evidence_id: str
    source_id: str
    source_title: str
    source_domain: str | None = None  # a web Source's host, for the inline citation
    published_at: datetime | None = None
    credibility_score: float | None = None
    passage: PassageRef
    passage_text: str
    page: int | None = None
    section_heading: str | None = None
    # ADR-0006: the primary citation of each premise of an analytical Claim, in order.
    premises: list["Citation"] = Field(default_factory=list)


UNVERIFIED_MARK = " [UNVERIFIED]"
