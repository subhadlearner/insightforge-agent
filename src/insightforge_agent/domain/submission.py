"""The Submission check (design.md section 2): which source types a Brief may use."""

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.plan import SourceType


def available_source_types(brief: Brief, has_eligible_reports: bool) -> list[SourceType]:
    """Enabled and available: web always, documents once any was uploaded for this Run,
    memory once the User has an eligible Report."""
    on = brief.source_toggles
    out: list[SourceType] = []
    if on.get("web"):
        out.append("web")
    if on.get("documents") and brief.document_ids:
        out.append("documents")
    if on.get("memory") and has_eligible_reports:
        out.append("memory")
    return out


def check_submission(brief: Brief, has_eligible_reports: bool) -> list[SourceType]:
    """The usable source types, or SubmissionRejected naming why there are none."""
    available = available_source_types(brief, has_eligible_reports)
    if available:
        return available
    enabled = [k for k, v in brief.source_toggles.items() if v]
    if not enabled:
        raise SubmissionRejected("every source type is switched off")
    reasons = {"documents": "no documents were uploaded", "memory": "no earlier Report is eligible"}
    raise SubmissionRejected("; ".join(reasons[k] for k in enabled))
