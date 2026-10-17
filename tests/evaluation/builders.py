"""Stored Reports for metric and judge tests: Sources, Observations and Passages are written
through the real SourceStore, and the Report body has the shape `complete` persists."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from insightforge_agent.domain.contracts import (
    Claim,
    ClaimVerdict,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportDraft,
    ReportSection,
    UNVERIFIED_MARK,
    VerifiedReport,
)
from insightforge_agent.domain.models import Report
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.source_store import PassageDraft, SourceStore

OWNER = "alice"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass
class World:
    repos: MemoryRepos = field(default_factory=MemoryRepos)

    @property
    def store(self) -> SourceStore:
        return SourceStore(self.repos.sources, self.repos.observations, self.repos.passages)

    def web(self, url: str, *paragraphs: str) -> list[PassageRef]:
        obs = self.store.ingest_web(owner_id=OWNER, run_id="r1", url=url, title=url,
                                    body="\n\n".join(paragraphs), fetched_at=T0)
        return [PassageRef(observation_id=obs.id, index=i) for i in range(len(paragraphs))]

    def pdf(self, name: str, *chunks: str) -> list[PassageRef]:
        obs = self.store.ingest_document(
            owner_id=OWNER, run_id="r1", content_hash=f"hash-{name}", filename=name,
            chunks=[PassageDraft(c, page=n + 1, section_heading="Results")
                    for n, c in enumerate(chunks)], fetched_at=T0)
        return [PassageRef(observation_id=obs.id, index=i) for i in range(len(chunks))]


def item(eid: str, primary: PassageRef, *others: PassageRef) -> EvidenceItem:
    return EvidenceItem(id=eid, subtask_id="s1", aspect_id="a1", statement=f"statement {eid}",
                        confidence="MEDIUM", supporting=[primary, *others], primary=primary)


def historical(hid: str, primary: PassageRef, day: date = date(2026, 3, 1)) -> HistoricalItem:
    return HistoricalItem(id=hid, subtask_id="m1", aspect_id="a2", heading="Earlier findings",
                          statement=f"statement {hid}", report_id="rep_old", run_id="run_old",
                          report_date=day, primary=primary)


def verdict(section: int, claim: int, eid: str, outcome: str, ref: PassageRef) -> ClaimVerdict:
    return ClaimVerdict(section=section, claim=claim, evidence_id=eid, verdict=outcome,
                        check="web_refetch", original=ref)


def report(sections: list[ReportSection], items: list[EvidenceItem] = (),
           background: list[HistoricalItem] = (), verdicts: list[ClaimVerdict] = (),
           summary: dict | None = None, gaps: list[str] = (), report_id: str = "rep_1") -> Report:
    """A Report as `complete` stores it. `summary` defaults to the counts of `verdicts`, as
    the Fact-Checker writes them."""
    draft = ReportDraft(title="Report", summary="An executive summary.", sections=sections,
                        gaps=list(gaps))
    marked = draft.model_copy(deep=True)
    for v in verdicts:
        if v.verdict == "unverified":
            c = marked.sections[v.section].claims[v.claim]
            c.text += UNVERIFIED_MARK
    if summary is None:
        counts = {k: sum(v.verdict == k for v in verdicts)
                  for k in ("verified", "unverified", "unchecked")}
        checked = counts["verified"] + counts["unverified"]
        summary = {"implemented": True, "claims": sum(len(s.claims) for s in sections),
                   "sampled": len(verdicts), **counts, "similarity_threshold": 0.85,
                   "pass_rate": counts["verified"] / checked if checked else None}
    verified = VerifiedReport(draft=marked, verdicts=list(verdicts), summary=summary)
    bundle = EvidenceBundle(sections=[EvidenceSection(aspect_id="a1", heading="Findings",
                                                      confidence="MEDIUM", items=list(items))])
    return Report(id=report_id, owner_id=OWNER, run_id="r1", created_at=T0, body={
        "draft": draft.model_dump(), "verified": verified.model_dump(),
        "evidence": bundle.model_dump(),
        "historical": HistoricalContext(items=list(background)).model_dump(mode="json")})


def section(heading: str, *claims: Claim) -> ReportSection:
    return ReportSection(heading=heading, claims=list(claims))


def claim(text: str, eid: str, historical_: bool = False) -> Claim:
    return Claim(text=text, evidence_id=eid, historical=historical_)
