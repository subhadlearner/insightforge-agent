"""Citation lookup: every Claim of a stored Report resolved to its primary Passage and Source.

Independent of the Fact-Checker's sample, so the citation drawer works for all Claims."""

from insightforge_agent.domain.contracts import Citation, EvidenceBundle, ReportDraft
from insightforge_agent.domain.models import Report
from insightforge_agent.stores.source_store import SourceStore


def report_citations(store: SourceStore, owner_id: str, report: Report) -> list[Citation]:
    """One Citation per Claim, in draft order. Uses the Evidence the Report was written from,
    so the primary Passage is the one the Claim was written against, not a later re-fetch."""
    draft = ReportDraft.model_validate(report.body["draft"])
    bundle = EvidenceBundle.model_validate(report.body["evidence"])
    items = {i.id: i for s in bundle.sections for i in s.items}
    return [
        store.citation(owner_id, items[c.evidence_id].primary).model_copy(update={
            "section": si, "claim": ci, "evidence_id": c.evidence_id})
        for si, s in enumerate(draft.sections) for ci, c in enumerate(s.claims)
    ]
