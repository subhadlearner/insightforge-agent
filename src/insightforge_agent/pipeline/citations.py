"""Citation lookup: every Claim of a stored Report resolved to its primary Passage and Source.

Independent of the Fact-Checker's sample, so the citation drawer works for all Claims. A
Historical Claim resolves to the original primary Passage it inherited from the earlier Report.
Any gap in that chain raises ProvenanceError: no Claim is ever skipped or given a stand-in."""

from typing import NamedTuple

from pydantic import ValidationError

from insightforge_agent.domain.contracts import (
    Citation,
    Claim,
    EvidenceBundle,
    HistoricalContext,
    ReportDraft,
)
from insightforge_agent.domain.errors import NotFoundError, ProvenanceError
from insightforge_agent.domain.models import Report
from insightforge_agent.stores.source_store import SourceStore


class ClaimCitation(NamedTuple):
    """One Claim of a Report and its Citation, or the ProvenanceError that stops it resolving."""

    section: int  # the section's index in the draft
    position: int  # the Claim's index in its section
    claim: Claim
    citation: Citation | ProvenanceError


def claim_citations(store: SourceStore, owner_id: str, report: Report) -> list[ClaimCitation]:
    """Every Claim, in draft order, resolved on its own: a Claim that does not resolve carries
    its ProvenanceError instead of a Citation. Raises ProvenanceError for a malformed body."""
    try:
        draft = ReportDraft.model_validate(report.body["draft"])
        bundle = EvidenceBundle.model_validate(report.body["evidence"])
        historical = HistoricalContext.model_validate(report.body.get("historical") or {})
    except (KeyError, ValidationError) as e:
        raise ProvenanceError(f"report {report.id}: malformed body: {e!r}") from e
    primary = {i.id: i.primary for s in bundle.sections for i in s.items}
    background = {h.id: h.primary for h in historical.items}
    out = []
    for si, s in enumerate(draft.sections):
        for ci, c in enumerate(s.claims):
            ref = (background if c.historical else primary).get(c.evidence_id)
            if ref is None:
                resolved: Citation | ProvenanceError = ProvenanceError(
                    f"report {report.id}: claim {si}.{ci} cites unknown item {c.evidence_id}")
            else:
                try:
                    resolved = store.citation(owner_id, ref).model_copy(
                        update={"section": si, "claim": ci, "evidence_id": c.evidence_id})
                except NotFoundError as e:
                    resolved = ProvenanceError(f"report {report.id}: claim {si}.{ci}: {e}")
                    resolved.__cause__ = e  # as `raise ... from e` would chain it
            out.append(ClaimCitation(si, ci, c, resolved))
    return out


def report_citations(store: SourceStore, owner_id: str, report: Report) -> list[Citation]:
    """One Citation per Claim, in draft order. Uses the Evidence the Report was written from,
    so the primary Passage is the one the Claim was written against, not a later re-fetch.
    Raises ProvenanceError when the Report's stored provenance does not resolve."""
    citations = []
    for found in claim_citations(store, owner_id, report):
        if isinstance(found.citation, ProvenanceError):
            raise found.citation
        citations.append(found.citation)
    return citations
