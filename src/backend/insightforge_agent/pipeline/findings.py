"""Normalise a Run's current Claims (as `BaselineSeed`s) into Findings (design.md section 8).

The vocabulary settles what it can; the rest goes to the Finding judge in bounded batches. Two
kinds of "no Finding" are kept apart:

- **Skipped**: the Claim is known not to qualify (ineligible, an inexact quantity, or the judge
  answered `unsupported`). Logged with the Diff; the Run goes on without it.
- **Failed**: the Claim could not be interpreted (an invalid reply, no answer for it, an answer
  that fails the checks, or no judge at all). `NormalizationFailed` is raised, so the attempt
  fails and the Diff retry lifecycle applies; an eligible Claim never silently counts as absent
  (Not reconfirmed). A model or transport error propagates the same way."""

from insightforge_agent.agents.finding_judge import FindingJudge
from insightforge_agent.agents.llm_json import BadModelReply
from insightforge_agent.domain.findings import (
    Finding,
    JudgeRequest,
    NormalizedRun,
    PredicateRegistry,
    Rejected,
    Skipped,
    finding_from_judgment,
    normalize_seed,
)
from insightforge_agent.domain.watchlist import BaselineSeed


class NormalizationFailed(RuntimeError):
    """Eligible Claims could not be interpreted; retry rather than diff without them."""


def normalize_seeds(
    seeds: list[BaselineSeed],
    run_id: str,
    registry: PredicateRegistry,
    judge: FindingJudge | None,
) -> NormalizedRun:
    findings: list[Finding] = []
    skipped: list[Skipped] = []
    requests: list[JudgeRequest] = []
    for index, seed in enumerate(seeds):
        result = normalize_seed(seed, index, run_id, registry)
        if isinstance(result, Finding):
            findings.append(result)
        elif isinstance(result, Skipped):
            skipped.append(result)
        else:
            requests.append(result)
    if not requests:
        return NormalizedRun(findings=findings, skipped=skipped)
    if judge is None:
        raise NormalizationFailed(
            f"{len(requests)} Claim(s) need the Finding judge; none is configured")
    try:
        judgments = judge.judge(requests)
    except BadModelReply as error:
        raise NormalizationFailed(f"the Finding judge's reply was invalid: {error}") from error
    failures: list[str] = []
    for request in requests:
        judgment = judgments.get(request.index)
        if judgment is None:
            failures.append(f"{request.seed.evidence.id}: no judgment")
            continue
        result = finding_from_judgment(request, judgment, registry)
        if isinstance(result, Rejected):
            failures.append(f"{result.evidence_id}: {result.reason}")
        elif isinstance(result, Finding):
            findings.append(result)
        else:
            skipped.append(result)
    if failures:
        raise NormalizationFailed("; ".join(failures)[:900])
    return NormalizedRun(findings=findings, skipped=skipped)
