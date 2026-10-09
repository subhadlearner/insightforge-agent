"""FACT_CHECKING: the stage's place in the topology, with no checking yet.

Sampling, re-fetching and Claim verdicts are a later ticket. Until then the draft passes
through and the summary says so, so nothing reads as verified that was not."""

from insightforge_agent.domain.contracts import ReportDraft, VerifiedReport


def fact_check(draft: ReportDraft) -> VerifiedReport:
    return VerifiedReport(draft=draft, summary={"implemented": False, "checked": 0})
