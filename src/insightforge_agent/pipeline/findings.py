"""Normalise a Run's current Claims (as `BaselineSeed`s) into Findings (design.md section 8).

The vocabulary settles what it can; the rest goes to the Finding judge in bounded batches. A
judge reply that is not valid JSON leaves those Claims skipped (never guessed); a model or
transport error propagates, so the caller retries the whole normalisation."""

import logging

from insightforge_agent.agents.finding_judge import FindingJudge
from insightforge_agent.agents.llm_json import BadModelReply
from insightforge_agent.domain.findings import (
    Finding,
    JudgeRequest,
    NormalizedRun,
    PredicateRegistry,
    Skipped,
    finding_from_judgment,
    normalize_seed,
)
from insightforge_agent.domain.watchlist import BaselineSeed

logger = logging.getLogger(__name__)


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
    if requests:
        judgments = {}
        if judge is None:
            reason = "no Finding judge is configured"
        else:
            try:
                judgments = judge.judge(requests)
                reason = "the judge gave no answer for this Claim"
            except BadModelReply as error:
                logger.warning("Finding judge reply rejected: %s", error)
                reason = "the judge's reply was invalid"
        for request in requests:
            judgment = judgments.get(request.index)
            if judgment is None:
                skipped.append(Skipped(evidence_id=request.seed.evidence.id,
                                       claim=request.seed.claim.text,
                                       reason=f"{request.reason}: {reason}"))
                continue
            result = finding_from_judgment(request, judgment, registry)
            (findings if isinstance(result, Finding) else skipped).append(result)
    return NormalizedRun(findings=findings, skipped=skipped)
