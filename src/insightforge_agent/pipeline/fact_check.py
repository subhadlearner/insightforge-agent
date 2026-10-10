"""FACT_CHECKING: sample Claims, re-fetch their Sources and give each sampled Claim a verdict.

Design.md section 7. Re-fetching a web Source stores a new Observation with its own Passages;
the original Observation, its Passages and the Claim's citation are never touched. The verdict
takes the maximum similarity between the Claim and any Passage of the re-fetch, and a numeric
Claim also needs its figures stated in the Passage that matched best. No model is called."""

import math
import random

from insightforge_agent.domain.contracts import (
    UNVERIFIED_MARK,
    ClaimVerdict,
    EvidenceBundle,
    PassageRef,
    ReportDraft,
    VerifiedReport,
)
from insightforge_agent.domain.models import Passage, Source
from insightforge_agent.domain.quantities import unsupported_numbers
from insightforge_agent.embeddings import cosine
from insightforge_agent.pipeline.deps import Deps


def sample_size(claims: int, rate: float, minimum: int) -> int:
    """20% of the Claims, at least `minimum`; every Claim when there are fewer than that."""
    return min(claims, max(minimum, math.ceil(rate * claims)))


def sample_positions(run_id: str, claims: int, rate: float, minimum: int) -> list[int]:
    """Which Claims (by position) are checked: the same ones every time for the same Run."""
    chosen = random.Random(run_id).sample(range(claims), sample_size(claims, rate, minimum))
    return sorted(chosen)


def _best_match(deps: Deps, claim: str, passages: list[Passage]) -> tuple[float, Passage | None]:
    if not passages:
        return 0.0, None
    vectors = deps.embedder.embed([claim] + [p.text for p in passages])
    scores = [cosine(vectors[0], v) for v in vectors[1:]]
    best = max(range(len(passages)), key=lambda i: (scores[i], -i))
    return scores[best], passages[best]


class _Refetcher:
    """Re-fetches each cited web Source at most once per fact-check."""

    def __init__(self, deps: Deps, owner_id: str, run_id: str) -> None:
        self._deps, self._owner, self._run = deps, owner_id, run_id
        self._done: dict[str, list[Passage] | None] = {}

    def passages(self, source: Source) -> list[Passage] | None:
        """The new Observation's Passages, or None when the re-fetch failed."""
        if source.id not in self._done:
            self._done[source.id] = self._fetch(source)
        return self._done[source.id]

    def _fetch(self, source: Source) -> list[Passage] | None:
        deps = self._deps
        try:
            page = deps.fetcher.fetch(source.locator)
        except Exception as e:  # noqa: BLE001 - any failure to re-fetch makes the Claim unchecked
            deps.log(self._owner, self._run, "fact_check_refetch_failed",
                     source_id=source.id, error=f"{type(e).__name__}: {e}")
            return None
        # Stored under the cited Source's own address, so a redirect cannot make a new Source.
        obs = deps.store.ingest_web(
            owner_id=self._owner, run_id=self._run, url=source.locator, title=page.title,
            body=page.body, fetched_at=deps.now(), credibility_score=source.credibility_score,
            published_at=page.published_at or source.published_at)
        deps.log(self._owner, self._run, "fact_check_refetched",
                 source_id=source.id, observation_id=obs.id)
        return deps.repos.passages.list(self._owner, obs.id)


def _verdict(
    deps: Deps, refetch: _Refetcher, owner_id: str, section: int, index: int, text: str,
    historical: bool, evidence_id: str, original: PassageRef,
) -> ClaimVerdict:
    base = dict(section=section, claim=index, evidence_id=evidence_id, original=original)
    if historical:
        return ClaimVerdict(
            **base, verdict="unchecked", check="not_checkable",
            reason="a Historical Claim is checked against its original Sources, which arrive "
                   "with the memory researcher")
    source = deps.repos.sources.get(
        owner_id, deps.repos.observations.get(owner_id, original.observation_id).source_id)
    if source.kind == "pdf":  # the stored chunks are the original text: no re-fetch
        check, passages = "document_passages", deps.repos.passages.list(
            owner_id, original.observation_id)
    else:
        check, passages = "web_refetch", refetch.passages(source)
        if passages is None:
            return ClaimVerdict(**base, verdict="unchecked", check=check,
                                reason="the Source could not be re-fetched")
    similarity, matched = _best_match(deps, text, passages)
    if matched is None:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=0.0,
                            reason="the Source has no Passages")
    ref = PassageRef(observation_id=matched.observation_id, index=matched.index)
    if similarity < deps.similarity_threshold:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=similarity,
                            matched=ref, reason="no Passage is similar enough to the Claim")
    missing = unsupported_numbers(text, matched.text)
    if missing:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=similarity,
                            matched=ref,
                            reason=f"the matched Passage does not state {sorted(missing)}")
    return ClaimVerdict(**base, verdict="verified", check=check, similarity=similarity,
                        matched=ref)


def summarise(total: int, verdicts: list[ClaimVerdict], threshold: float) -> dict:
    counts = {v: sum(1 for x in verdicts if x.verdict == v)
              for v in ("verified", "unverified", "unchecked")}
    checked = counts["verified"] + counts["unverified"]
    return {
        "implemented": True, "claims": total, "sampled": len(verdicts),
        **counts, "similarity_threshold": threshold,
        # `unchecked` Claims are left out: they say nothing about the Report's accuracy.
        "pass_rate": counts["verified"] / checked if checked else None,
    }


def fact_check(
    deps: Deps, owner_id: str, run_id: str, draft: ReportDraft, bundle: EvidenceBundle,
) -> VerifiedReport:
    items = {i.id: i for s in bundle.sections for i in s.items}
    claims = [(si, ci, c) for si, s in enumerate(draft.sections) for ci, c in enumerate(s.claims)]
    refetch = _Refetcher(deps, owner_id, run_id)
    verdicts = [
        _verdict(deps, refetch, owner_id, si, ci, c.text, c.historical, c.evidence_id,
                 items[c.evidence_id].primary)
        for si, ci, c in (claims[p] for p in sample_positions(
            run_id, len(claims), deps.fact_check_rate, deps.fact_check_minimum))
    ]
    unverified = {(v.section, v.claim) for v in verdicts if v.verdict == "unverified"}
    marked = draft.model_copy(update={"sections": [
        s.model_copy(update={"claims": [
            c.model_copy(update={"text": c.text + UNVERIFIED_MARK}) if (si, ci) in unverified else c
            for ci, c in enumerate(s.claims)]})
        for si, s in enumerate(draft.sections)]})
    summary = summarise(len(claims), verdicts, deps.similarity_threshold)
    deps.log(owner_id, run_id, "fact_check_summary", **summary)
    return VerifiedReport(draft=marked, verdicts=verdicts, summary=summary)
