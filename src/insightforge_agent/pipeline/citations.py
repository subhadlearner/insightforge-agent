"""Citation lookup: every Claim of a stored Report resolved to its primary Passage and Source.

Independent of the Fact-Checker's sample, so the citation drawer works for all Claims. A
Historical Claim resolves to the original primary Passage it inherited from the earlier Report.
Any gap in that chain raises ProvenanceError: no Claim is ever skipped or given a stand-in."""

from pydantic import ValidationError

from insightforge_agent.domain.contracts import (
    Citation,
    EvidenceBundle,
    HistoricalContext,
    ReportDraft,
)
from insightforge_agent.domain.errors import NotFoundError, ProvenanceError
from insightforge_agent.domain.models import Report
from insightforge_agent.stores.source_store import SourceStore


def report_citations(store: SourceStore, owner_id: str, report: Report) -> list[Citation]:
    """One Citation per Claim, in draft order. Uses the Evidence the Report was written from,
    so the primary Passage is the one the Claim was written against, not a later re-fetch.
    Raises ProvenanceError when the Report's stored provenance does not resolve."""
    try:
        draft = ReportDraft.model_validate(report.body["draft"])
        bundle = EvidenceBundle.model_validate(report.body["evidence"])
        historical = HistoricalContext.model_validate(report.body.get("historical") or {})
    except (KeyError, ValidationError) as e:
        raise ProvenanceError(f"report {report.id}: malformed body: {e!r}") from e
    primary = {i.id: i.primary for s in bundle.sections for i in s.items}
    background = {h.id: h.primary for h in historical.items}
    citations = []
    for si, s in enumerate(draft.sections):
        for ci, c in enumerate(s.claims):
            ref = (background if c.historical else primary).get(c.evidence_id)
            if ref is None:
                raise ProvenanceError(f"report {report.id}: claim {si}.{ci} cites unknown "
                                      f"item {c.evidence_id}")
            try:
                found = store.citation(owner_id, ref)
            except NotFoundError as e:
                raise ProvenanceError(f"report {report.id}: claim {si}.{ci}: {e}") from e
            citations.append(found.model_copy(
                update={"section": si, "claim": ci, "evidence_id": c.evidence_id}))
    return citations
