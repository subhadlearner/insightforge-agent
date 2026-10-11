"""FACT_CHECKING: sample Claims, re-fetch their Sources and give each sampled Claim a verdict.

Design.md section 7. Re-fetching a web Source stores a new Observation with its own Passages;
the original Observation, its Passages and the Claim's citation are never touched.

Verification (F2, owner-approved; docs/audits/2026-10-11-f2-fact-checker.md):
1. Spans: each sentence of the re-fetched (or stored document) Passages, and each window of
   adjacent sentences: two, or as many as the Claim has, up to four (`candidate_spans`,
   `window_width`). A span is cited as its parent Passage.
2. Relevance: the embedder scores the Claim against every span; spans under the similarity
   threshold are dropped.
3. Figures: a span that does not state every figure of the Claim is dropped.
4. Support: one light-model entailment call judges the best `TOP_K` remaining spans. The Claim
   is verified only if a span is judged SUPPORTED. No span left, or a failed or unusable reply,
   leaves it unverified (fail closed: never unchecked, which the pass rate leaves out)."""

import math
import random
from collections import Counter
from dataclasses import dataclass

from insightforge_agent.agents.llm_json import ask_json
from insightforge_agent.domain.contracts import (
    UNVERIFIED_MARK,
    ClaimVerdict,
    EvidenceBundle,
    PassageRef,
    ReportDraft,
    VerifiedReport,
)
from insightforge_agent.domain.models import Passage, Source
from insightforge_agent.domain.passages import (
    Span,
    Window,
    candidate_spans,
    render_batch,
    window_width,
)
from insightforge_agent.domain.quantities import unsupported_numbers
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.embeddings import cosine
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.entailment import ENTAIL_SYSTEM, EntailmentVerdicts

TOP_K = 3  # spans judged by the one entailment call per Claim


def sample_size(claims: int, rate: float, minimum: int) -> int:
    """20% of the Claims, at least `minimum`; every Claim when there are fewer than that."""
    return min(claims, max(minimum, math.ceil(rate * claims)))


def sample_positions(run_id: str, claims: int, rate: float, minimum: int) -> list[int]:
    """Which Claims (by position) are checked: the same ones every time for the same Run."""
    chosen = random.Random(run_id).sample(range(claims), sample_size(claims, rate, minimum))
    return sorted(chosen)


def _ref(span: Span) -> PassageRef:
    return PassageRef(observation_id=span.passage.observation_id, index=span.passage.index)


@dataclass(frozen=True)
class Screened:
    """The deterministic part of a verdict (steps 1-3). `candidates` are the spans, best first,
    that the entailment call judges, each with the similarity computed for that exact span;
    when there are none, `reason` says why. `similarity` and `matched` describe the best
    candidate, else the best span the gates rejected."""

    similarity: float
    matched: PassageRef | None
    candidates: list[tuple[float, Span]]
    reason: str = ""


class Vectors:
    """Embeds each distinct text once per fact-check: the spans of a Source cited by several
    sampled Claims are not embedded again."""

    def __init__(self, deps: Deps) -> None:
        self._embedder, self._known = deps.embedder, {}

    def of(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._known]
        if missing:
            self._known.update(zip(missing, self._embedder.embed(missing), strict=True))
        return [self._known[t] for t in texts]


def screen(deps: Deps, claim: str, passages: list[Passage],
           vectors: Vectors | None = None) -> Screened:
    spans = candidate_spans(passages, window_width(claim))
    if not spans:
        return Screened(0.0, None, [], "the Source has no Passages")
    claim_vector, *span_vectors = (vectors or Vectors(deps)).of([claim] + [s.text for s in spans])
    scores = [cosine(claim_vector, v) for v in span_vectors]
    ranked = sorted(zip(scores, range(len(spans)), spans), key=lambda x: (-x[0], x[1]))
    similar = [(score, span) for score, _, span in ranked if score >= deps.similarity_threshold]
    if not similar:
        best, _, span = ranked[0]
        return Screened(best, _ref(span), [], "no Passage is similar enough to the Claim")
    stated = [(score, span) for score, span in similar if not unsupported_numbers(claim, span.text)]
    if not stated:
        best, span = similar[0]
        missing = sorted(unsupported_numbers(claim, span.text))
        return Screened(best, _ref(span), [], f"the matched Passage does not state {missing}")
    # One place per group of overlapping spans, so a sentence and its own windows cannot fill
    # every place and keep a supporting sentence elsewhere from the judge. A group is
    # represented by the widest span that contains its first (best) span and also passes the
    # gates, so the judge sees the context: "It sold 3.02 million..." with the sentence that
    # names "BYD", or "Ltd. sold..." rejoined with "BYD Co.". Each representative carries its
    # own similarity, and the best `TOP_K` representatives by that similarity are judged.
    def within(inner: Span, outer: Span) -> bool:
        return outer.overlaps(inner) and outer.first <= inner.first and inner.last <= outer.last

    groups: list[tuple[float, Span]] = []
    for score, span in stated:
        n = next((i for i, (_, g) in enumerate(groups) if span.overlaps(g)), None)
        if n is None:
            groups.append((score, span))
        elif within(groups[n][1], span):  # widen that group; fold in any other it covers
            groups = [(score, span) if i == n else g for i, g in enumerate(groups)
                      if i == n or not within(g[1], span)]
    chosen = sorted(groups, key=lambda g: -g[0])[:TOP_K]  # stable: ties keep their order
    return Screened(chosen[0][0], _ref(chosen[0][1]), chosen)


def _entailed(deps: Deps, owner_id: str, run_id: str, statement: str,
              candidates: list[tuple[float, Span]],
              **where) -> tuple[tuple[float, Span] | None, str]:
    """Step 4: the best candidate judged SUPPORTED, or None and the reason none is. The
    request is held to the per-call cap by dropping the weakest candidates; if even the best
    one alone does not fit, no call is made. `where` names the Claim in the logged events."""
    def payload(cands: list[tuple[float, Span]]) -> str:
        return f"Statement: {statement}\n\n" + render_batch(
            [Window(span.passage, span.text) for _, span in cands])

    system_tokens = estimate_tokens(ENTAIL_SYSTEM)

    def size(cands: list[tuple[float, Span]]) -> int:  # the whole request, system prompt included
        return system_tokens + estimate_tokens(payload(cands))

    while len(candidates) > 1 and size(candidates) > deps.passage_token_cap:
        candidates = candidates[:-1]
    user, tokens = payload(candidates), size(candidates)
    if tokens > deps.passage_token_cap:  # never exceed the cap: fail closed without a call
        deps.log(owner_id, run_id, "fact_check_entailment_skipped", **where, tokens=tokens,
                 passage_token_cap=deps.passage_token_cap)
        return None, (f"the entailment request ({tokens} tokens) exceeds the per-call cap of "
                      f"{deps.passage_token_cap}")
    deps.spend(owner_id, run_id, ENTAIL_SYSTEM, user)
    try:
        reply = ask_json(deps.light_model, ENTAIL_SYSTEM, user, EntailmentVerdicts)
    except Exception as e:  # noqa: BLE001 - fail closed: without a judgment nothing is supported
        deps.log(owner_id, run_id, "fact_check_entailment_failed", **where,
                 error=f"{type(e).__name__}: {e}"[:300])
        return None, f"the entailment check failed ({type(e).__name__})"
    judged: dict[int, set[str]] = {}
    for v in reply.verdicts:
        if 1 <= v.passage <= len(candidates):
            judged.setdefault(v.passage, set()).add(v.verdict)
    deps.log(owner_id, run_id, "fact_check_entailment", **where, candidates=len(candidates),
             verdicts={str(n): sorted(vs) for n, vs in sorted(judged.items())})
    # Only an unambiguous SUPPORTED counts: a span also judged otherwise does not support.
    supported = [n for n in sorted(judged) if judged[n] == {"SUPPORTED"}]
    if supported:
        return candidates[supported[0] - 1], ""
    if not judged:
        return None, "the entailment reply judged none of the candidate spans"
    counts = Counter(verdict for vs in judged.values() for verdict in vs)
    return None, f"no candidate span was judged SUPPORTED ({dict(sorted(counts.items()))})"


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
    deps: Deps, refetch: _Refetcher, vectors: Vectors, owner_id: str, run_id: str,
    section: int, index: int, text: str, historical: bool, evidence_id: str, original: PassageRef,
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
    s = screen(deps, text, passages, vectors)
    supported, reason = (None, s.reason) if not s.candidates else _entailed(  # no call if none
        deps, owner_id, run_id, text, s.candidates, section=section, claim=index)
    if supported is None:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched, reason=reason)
    similarity, span = supported
    return ClaimVerdict(**base, verdict="verified", check=check, similarity=similarity,
                        matched=_ref(span))


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
    refetch, vectors = _Refetcher(deps, owner_id, run_id), Vectors(deps)
    verdicts = [
        _verdict(deps, refetch, vectors, owner_id, run_id, si, ci, c.text, c.historical,
                 c.evidence_id, items[c.evidence_id].primary)
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
