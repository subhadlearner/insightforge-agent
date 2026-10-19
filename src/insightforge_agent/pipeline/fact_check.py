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
   leaves it unverified (fail closed: never unchecked, which the pass rate leaves out).

Counterpart fallback (T18 CS1A): only when steps 2-3 leave no candidate for a web Claim, the
re-fetched Passage whose normalised text equals the cited Passage's (one, or the one at the
cited index; never a near match) is screened again without the similarity floor, keeping the
figure gate, grouping, TOP_K and step 4 unchanged. If it is absent, ambiguous, stored under
another Source, or has no span stating the figures, the verdict is exactly the F2 one. Uploaded
documents keep their own check. Each attempt is logged as `fact_check_counterpart`.

A Historical Claim (T8) goes through the same steps against its original Sources: its
Historical item carries the earlier Report's primary Passage, and the verdict's check kind is
`historical_web_refetch` or `historical_document_passages`."""

import math
import random
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from insightforge_agent.agents.llm_json import ask_json
from insightforge_agent.domain.contracts import (
    UNVERIFIED_MARK,
    ClaimVerdict,
    EvidenceBundle,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportDraft,
    VerifiedReport,
)
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.memory import without_report_date
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


class _Group(NamedTuple):
    """Overlapping candidate spans that take one place in the entailment call."""

    rank: float  # the best similarity that established the group: decides TOP_K, nothing else
    similarity: float  # the similarity of `span` itself: what a verdict reports
    span: Span  # the representative: what the judge sees


@dataclass(frozen=True)
class Screened:
    """The deterministic part of a verdict (steps 1-3). `candidates` are the spans, in group
    rank order, that the entailment call judges, each with the similarity computed for that
    exact span (never the group's rank); when there are none, `reason` says why.
    `similarity` and `matched` describe the first (highest-ranked) candidate, with its own
    similarity, else the best span the gates rejected."""

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
           vectors: Vectors | None = None, *, floor: float | None = None) -> Screened:
    """Steps 1-3. `floor` replaces the similarity threshold (the counterpart fallback screens
    its one Passage with no floor); every other step is the same."""
    spans = candidate_spans(passages, window_width(claim))
    if not spans:
        return Screened(0.0, None, [], "the Source has no Passages")
    claim_vector, *span_vectors = (vectors or Vectors(deps)).of([claim] + [s.text for s in spans])
    scores = [cosine(claim_vector, v) for v in span_vectors]
    ranked = sorted(zip(scores, range(len(spans)), spans), key=lambda x: (-x[0], x[1]))
    threshold = deps.similarity_threshold if floor is None else floor
    similar = [(score, span) for score, _, span in ranked if score >= threshold]
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
    # represented by the widest span that contains its first (best) span and also passes both
    # gates, so the judge sees the context: "It sold 3.02 million..." with the sentence that
    # names "BYD", or "Ltd. sold..." rejoined with "BYD Co.". A span widens only when it
    # contains every group it touches, which then become one, so no sentence is shown twice.
    # Widening never lowers a group's place: groups enter TOP_K by `rank`, while the judge
    # sees `span` and verdicts report `similarity`, both of the representative itself.
    groups: list[_Group] = []
    for score, span in stated:
        hit = [i for i, g in enumerate(groups) if span.overlaps(g.span)]
        if not hit:
            groups.append(_Group(rank=score, similarity=score, span=span))
        elif all(span.contains(groups[i].span) for i in hit):
            merged = _Group(rank=max(groups[i].rank for i in hit), similarity=score, span=span)
            groups = [merged if i == hit[0] else g for i, g in enumerate(groups)
                      if i not in hit[1:]]
    chosen = [(g.similarity, g.span) for g in sorted(groups, key=lambda g: -g.rank)[:TOP_K]]
    return Screened(chosen[0][0], _ref(chosen[0][1]), chosen)  # stable sort: ties keep order


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


def _normalised(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().casefold()


def counterpart(original: Passage, refetched: list[Passage]) -> tuple[Passage | None, str]:
    """The re-fetched Passage that is the cited one, by content: the only one whose normalised
    text (NFKC, whitespace, case) equals it, or among several equal ones the one at the cited
    index. Returns it with how it was found, or None with "absent" or "ambiguous"."""
    same = [p for p in refetched if _normalised(p.text) == _normalised(original.text)]
    if len(same) == 1:
        return same[0], "unique"
    at_index = [p for p in same if p.index == original.index]
    if len(at_index) == 1:
        return at_index[0], "unique_by_index"
    return None, "ambiguous" if same else "absent"


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
    section: int, index: int, text: str, evidence_id: str, original: PassageRef,
    historical: bool = False,
) -> ClaimVerdict:
    """`original` is the Passage the Claim cites. For a Historical Claim it is the original
    primary Passage inherited from the earlier Report, and `text` is the Claim without its
    inline report date; the check is the same and its kind says it was inherited."""
    base = dict(section=section, claim=index, evidence_id=evidence_id, original=original)
    prefix = "historical_" if historical else ""
    source = deps.repos.sources.get(
        owner_id, deps.repos.observations.get(owner_id, original.observation_id).source_id)
    if source.kind == "pdf":  # the stored chunks are the original text: no re-fetch
        check, passages = f"{prefix}document_passages", deps.repos.passages.list(
            owner_id, original.observation_id)
    else:
        check, passages = f"{prefix}web_refetch", refetch.passages(source)
        if passages is None:
            return ClaimVerdict(**base, verdict="unchecked", check=check,
                                reason="the Source could not be re-fetched")
    s = screen(deps, text, passages, vectors)
    if not s.candidates and source.kind != "pdf" and deps.fact_check_counterpart:
        fallback = _counterpart_verdict(deps, vectors, owner_id, run_id, section, index, text,
                                        original, source, passages, check, base)
        if fallback is not None:
            return fallback
    supported, reason = (None, s.reason) if not s.candidates else _entailed(  # no call if none
        deps, owner_id, run_id, text, s.candidates, section=section, claim=index)
    if supported is None:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched, reason=reason)
    similarity, span = supported
    return ClaimVerdict(**base, verdict="verified", check=check, similarity=similarity,
                        matched=_ref(span))


def _counterpart_verdict(
    deps: Deps, vectors: Vectors, owner_id: str, run_id: str, section: int, index: int,
    text: str, original: PassageRef, source: Source, refetched: list[Passage], check: str,
    base: dict,
) -> ClaimVerdict | None:
    """The fallback's verdict, or None when the counterpart cannot be used (the caller then
    returns F2's verdict unchanged). Logged either way."""
    def log(match: str, found: Passage | None = None, **extra) -> None:
        deps.log(owner_id, run_id, "fact_check_counterpart", section=section, claim=index,
                 original=original.model_dump(), match=match,
                 counterpart=_ref_of(found).model_dump() if found else None, **extra)

    if not refetched or {deps.repos.observations.get(owner_id, p.observation_id).source_id
                         for p in refetched} != {source.id}:
        log("source_mismatch", used=False)
        return None
    cited = deps.store.get_passages(owner_id, original.observation_id, [original.index])[0]
    found, match = counterpart(cited, refetched)
    if found is None:
        log(match, used=False)
        return None
    s = screen(deps, text, [found], vectors, floor=-1.0)
    if not s.candidates:  # no span of it states every figure of the Claim
        log(match, found, used=False, reason=s.reason)
        return None
    supported, reason = _entailed(deps, owner_id, run_id, text, s.candidates, section=section,
                                  claim=index)
    log(match, found, used=True, verified=supported is not None,
        counterpart_similarity=round(s.similarity, 6))
    if supported is None:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched, reason=reason)
    similarity, span = supported
    return ClaimVerdict(**base, verdict="verified", check=check, similarity=similarity,
                        matched=_ref(span))


def _ref_of(p: Passage) -> PassageRef:
    return PassageRef(observation_id=p.observation_id, index=p.index)


def _historical_verdict(
    deps: Deps, refetch: _Refetcher, vectors: Vectors, owner_id: str, run_id: str,
    section: int, index: int, text: str, item: HistoricalItem | None, evidence_id: str,
) -> ClaimVerdict:
    """A Historical Claim follows its inheritance chain: the Historical item names the original
    primary Passage, whose Observation names the original Source, checked as a current Claim
    would be (re-fetched if a web page, its stored chunks if an uploaded document)."""
    if item is None:  # the Writer's checks make this unreachable; never guess a Source
        return ClaimVerdict(section=section, claim=index, evidence_id=evidence_id,
                            original=PassageRef(observation_id="", index=0),
                            verdict="unchecked", check="not_checkable",
                            reason="the Historical Claim names no known Historical item")
    try:
        return _verdict(deps, refetch, vectors, owner_id, run_id, section, index,
                        without_report_date(text, item.report_date), evidence_id, item.primary,
                        historical=True)
    except NotFoundError:
        return ClaimVerdict(section=section, claim=index, evidence_id=evidence_id,
                            original=item.primary, verdict="unchecked", check="not_checkable",
                            reason="the original Source of the Historical Claim is no longer "
                                   "stored")


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
    historical: HistoricalContext | None = None,
) -> VerifiedReport:
    items = {i.id: i for s in bundle.sections for i in s.items}
    background = (historical or HistoricalContext()).by_id()
    claims = [(si, ci, c) for si, s in enumerate(draft.sections) for ci, c in enumerate(s.claims)]
    refetch, vectors = _Refetcher(deps, owner_id, run_id), Vectors(deps)
    verdicts = [
        _historical_verdict(deps, refetch, vectors, owner_id, run_id, si, ci, c.text,
                            background.get(c.evidence_id), c.evidence_id) if c.historical else
        _verdict(deps, refetch, vectors, owner_id, run_id, si, ci, c.text, c.evidence_id,
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
