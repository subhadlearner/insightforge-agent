"""FACT_CHECKING: sample Claims, re-fetch their Sources and give each sampled Claim a verdict.

Design.md section 7. Re-fetching a web Source stores a new Observation with its own Passages;
the original Observation, its Passages and the Claim's citation are never touched.

Verification (F2, owner-approved; docs/audits/2026-10-11-f2-fact-checker.md; ADR-0006 §7):
1. Spans: each sentence of the re-fetched (or stored document) Passages, and each window of
   adjacent sentences: two, or as many as the Claim has, up to four (`candidate_spans`,
   `window_width`). A span is cited as its parent Passage.
2. Relevance: the embedder scores the Claim against every span; spans under the similarity
   threshold are not added as candidates. Similarity retrieves and orders; it is not a verdict.
3. Figures: a span that does not state every figure of the Claim is never sent.
4. The cited Passage itself is always a candidate: its best span, screened with no similarity
   floor (step 3 still applies). For an uploaded document that is the stored chunk; for a web
   page, the re-fetched Passage whose normalised text equals the cited one (T18 CS1A
   `counterpart`: unique, or the one at the cited index; never a near match), logged as
   `fact_check_counterpart`.
5. Support: one call of the evidence-bound support judgment (`pipeline/support`, the same
   contract Synthesis uses) over the cited Passage's span and the best `TOP_K` screened spans,
   each shown in the context of its own Passage (`in_context`, up to `CONTEXT_TOKENS`; never
   another Passage or the Source title), with identity context for an uploaded document whose
   own text establishes its company. The weakest candidates are dropped until the request fits
   the per-call cap; if even the first does not fit, no call is made. The Claim is verified only
   if a candidate is SUPPORTED after the support map's checks. No candidate, or a failed or
   unusable reply, leaves it unverified (fail closed: never unchecked, which the pass rate
   leaves out).

Citation truth versus page drift: when a web page no longer holds the cited Passage, the
Passage as it was cited is judged in the same call but cannot verify the Claim; its verdict is
kept as `cited_support`, and a Claim its citation supported but the page no longer does is
reported as page drift.

An analytical Claim with premises (ADR-0006 §6) is judged once against all its premises
together (`premises_joint`): each premise's primary Passage as stored, or its counterpart on
the re-fetched page, else that page's best span for the premise's own statement.

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

from insightforge_agent.domain.contracts import (
    UNVERIFIED_MARK,
    ClaimVerdict,
    EvidenceBundle,
    EvidenceItem,
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
    candidate_spans,
    in_context,
    window_width,
)
from insightforge_agent.domain.provenance import document_entity
from insightforge_agent.domain.quantities import unsupported_numbers
from insightforge_agent.embeddings import cosine
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.support import (
    Judgment,
    Unit,
    judge_joint,
    judge_units,
    passage_units,
    request_tokens,
)

TOP_K = 3  # spans judged by the one entailment call per Claim
# The context target per judged span: context is added only while the span and its context fit
# it. A span longer than it is still shown whole (a span is indivisible), so this is a target,
# not a limit; the hard limit is `passage_token_cap` on the whole request, which `_entailed`
# enforces by dropping candidates, or making no call at all.
CONTEXT_TOKENS = 800


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


def judged_candidates(candidates: list[tuple[float, Span]]) -> list[tuple[float, Span]]:
    """The candidates the judge is shown, best first: one per distinct text in context, so two
    spans of a short Passage, each shown as the whole Passage, take one place."""
    seen: set[tuple[str, int, str]] = set()
    out = []
    for score, span in candidates:
        key = (span.passage.observation_id, span.passage.index, in_context(span, CONTEXT_TOKENS))
        if key not in seen:
            seen.add(key)
            out.append((score, span))
    return out


@dataclass(frozen=True)
class _Candidate:
    """One unit the judge is shown, with the similarity screening gave its span."""

    unit: Unit
    similarity: float


def _shown(candidates: list[tuple[float, Span]], entity: str | None) -> list[_Candidate]:
    """Screened spans as the judge is shown them: each in the context of its own Passage
    (`in_context`, at most `CONTEXT_TOKENS`), one per distinct text."""
    return [_Candidate(Unit(_ref(span), in_context(span, CONTEXT_TOKENS), entity), score)
            for score, span in judged_candidates(candidates)]


def _one_call(statement: str, candidates: list[_Candidate], cap: int) -> list[_Candidate]:
    """The candidates one request can hold: the weakest (last) dropped until it fits the
    per-call cap; empty when even the first does not fit."""
    while len(candidates) > 1 and request_tokens(statement, [c.unit for c in candidates]) > cap:
        candidates = candidates[:-1]
    if candidates and request_tokens(statement, [c.unit for c in candidates]) > cap:
        return []
    return candidates


def _judged(deps: Deps, owner_id: str, run_id: str, statement: str, units: list[Unit],
            **where) -> list[Judgment] | str:
    """The support judgments of `units`, or the reason there are none. Any failure fails
    closed: without a judgment nothing is supported."""
    try:
        return judge_units(deps, owner_id, run_id, statement, units, **where)
    except Exception as e:  # noqa: BLE001 - fail closed, as F2 did
        deps.log(owner_id, run_id, "fact_check_entailment_failed", **where,
                 error=f"{type(e).__name__}: {e}"[:300])
        return f"the support check failed ({type(e).__name__})"


def _not_supported(judgments: list[Judgment]) -> str:
    counts = Counter(j.verdict for j in judgments)
    return f"no candidate was judged SUPPORTED ({dict(sorted(counts.items()))})"


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


_NO_LONGER = {
    "absent": "the re-fetched page no longer holds the cited Passage (page drift)",
    "ambiguous": "the re-fetched page holds the cited Passage's text more than once, so it "
                 "could not be matched",
    "source_mismatch": "the re-fetch was stored under another Source",
    "off": "the cited Passage was not looked up on the re-fetched page",
}


def _verdict(
    deps: Deps, refetch: _Refetcher, vectors: Vectors, owner_id: str, run_id: str,
    section: int, index: int, text: str, evidence_id: str, original: PassageRef,
    historical: bool = False,
) -> ClaimVerdict:
    """`original` is the Passage the Claim cites. For a Historical Claim it is the original
    primary Passage inherited from the earlier Report, and `text` is the Claim without its
    inline report date; the check is the same and its kind says it was inherited.

    Candidates (ADR-0006 §7), in one bounded call: the best span of the cited Passage itself,
    screened with no similarity floor (the stored chunk of an uploaded document; for a web page
    its exact counterpart on the re-fetched page), then up to `TOP_K` spans screened as F2 does.
    Similarity only adds and orders candidates; it never stops the cited Passage being judged.
    A span that does not state every figure of the Claim is never sent. A web Claim is verified
    only by the re-fetched page; when the page does not hold the cited Passage, the Passage as
    it was cited is judged too (last, and dropped first to fit the cap) and recorded as
    `cited_support`, so page drift is told apart from a Claim its citation never supported."""
    base = dict(section=section, claim=index, evidence_id=evidence_id, original=original)
    prefix = "historical_" if historical else ""
    source = deps.repos.sources.get(
        owner_id, deps.repos.observations.get(owner_id, original.observation_id).source_id)
    pdf = source.kind == "pdf"
    if pdf:  # the stored chunks are the original text: no re-fetch
        check, passages = f"{prefix}document_passages", deps.repos.passages.list(
            owner_id, original.observation_id)
    else:
        check, passages = f"{prefix}web_refetch", refetch.passages(source)
        if passages is None:
            return ClaimVerdict(**base, verdict="unchecked", check=check,
                                reason="the Source could not be re-fetched")
    entity = document_entity(source.title, [p.text for p in passages]) if pdf else None
    try:
        cited = deps.store.get_passages(owner_id, original.observation_id, [original.index])[0]
    except NotFoundError:
        cited = None
    current, match = (cited, "stored") if pdf else (None, "off")
    if not pdf and cited is None:
        deps.log(owner_id, run_id, "fact_check_counterpart", section=section, claim=index,
                 original=original.model_dump(), match="cited_missing", counterpart=None)
    elif not pdf and deps.fact_check_counterpart:
        current, match = _counterpart(deps, owner_id, run_id, section, index, cited, source,
                                      passages)
    s = screen(deps, text, passages, vectors)

    def best(p: Passage, who: str | None) -> list[_Candidate]:
        return _shown(screen(deps, text, [p], vectors, floor=-1.0).candidates[:1], who)

    own = best(current, entity) if current is not None else []
    seen = {(c.unit.ref, c.unit.text) for c in own}
    candidates = own + [c for c in _shown(s.candidates, entity)
                        if (c.unit.ref, c.unit.text) not in seen]
    as_cited = best(cited, None) if not pdf and cited is not None and current is None else []
    if not candidates and not as_cited:
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched, reason=s.reason or "no candidate to judge",
                            cited_support=None if cited is None else "INSUFFICIENT_EVIDENCE")
    fitted = _one_call(text, candidates + as_cited, deps.passage_token_cap)
    if not fitted:
        tokens = request_tokens(text, [(candidates or as_cited)[0].unit])
        deps.log(owner_id, run_id, "fact_check_entailment_skipped", section=section,
                 claim=index, tokens=tokens, passage_token_cap=deps.passage_token_cap)
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched,
                            reason=(f"the support request ({tokens} tokens) exceeds the per-call "
                                    f"cap of {deps.passage_token_cap}"))
    judged = _judged(deps, owner_id, run_id, text, [c.unit for c in fitted], section=section,
                     claim=index)
    if isinstance(judged, str):
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched, reason=judged)
    current_n = sum(1 for c in fitted if c not in as_cited)
    now, then = judged[:current_n], judged[current_n:]
    cited_support = (then[0].verdict if then else
                     now[0].verdict if own and fitted[0] is own[0] else None)
    deps.log(owner_id, run_id, "fact_check_support", section=section, claim=index,
             candidates=current_n, cited_judged=bool(own) or bool(then),
             verdicts=[j.verdict for j in now], cited_support=cited_support)
    hit = next((n for n, j in enumerate(now) if j.verdict == "SUPPORTED"), None)
    if hit is None:
        reason = _not_supported(now) if now else (s.reason or "no candidate on the page")
        if then and cited_support == "SUPPORTED":
            reason = (f"the cited Passage supported the Claim when it was cited, but "
                      f"{_NO_LONGER.get(match, _NO_LONGER['absent'])}; {reason}")
        return ClaimVerdict(**base, verdict="unverified", check=check, similarity=s.similarity,
                            matched=s.matched, reason=reason, cited_support=cited_support)
    return ClaimVerdict(**base, verdict="verified", check=check,
                        similarity=fitted[hit].similarity, matched=now[hit].ref,
                        cited_support=cited_support)


def _counterpart(deps: Deps, owner_id: str, run_id: str, section: int, index: int,
                 cited: Passage, source: Source, refetched: list[Passage],
                 ) -> tuple[Passage | None, str]:
    """The cited Passage on the re-fetched page (T18 CS1A `counterpart`: equal normalised text,
    unique or at the cited index) and how it was found, or None and why not ("absent",
    "ambiguous", "source_mismatch"). Logged either way as `fact_check_counterpart`."""
    def log(match: str, found: Passage | None = None) -> None:
        deps.log(owner_id, run_id, "fact_check_counterpart", section=section, claim=index,
                 original=_ref_of(cited).model_dump(), match=match,
                 counterpart=_ref_of(found).model_dump() if found else None)

    if not refetched or {deps.repos.observations.get(owner_id, p.observation_id).source_id
                         for p in refetched} != {source.id}:
        log("source_mismatch")
        return None, "source_mismatch"
    found, match = counterpart(cited, refetched)
    log(match, found)
    return found, match


def _joint_verdict(
    deps: Deps, refetch: _Refetcher, vectors: Vectors, owner_id: str, run_id: str,
    section: int, index: int, text: str, items: list[EvidenceItem],
) -> ClaimVerdict:
    """A sampled analytical Claim (ADR-0006 §6), judged against all its premises together: for
    each premise, its primary Passage as stored (an uploaded document) or its counterpart on the
    re-fetched page, else that page's best span for the premise's own statement."""
    base = dict(section=section, claim=index, evidence_id=items[0].id,
                original=items[0].primary, check="premises_joint")
    units: list[Unit] = []
    for item in items:
        ref = item.primary
        try:
            source = deps.repos.sources.get(
                owner_id, deps.repos.observations.get(owner_id, ref.observation_id).source_id)
            cited = deps.store.get_passages(owner_id, ref.observation_id, [ref.index])[0]
        except NotFoundError:  # never guess a premise: this Claim cannot be checked
            return ClaimVerdict(**{**base, "check": "not_checkable"}, verdict="unchecked",
                                reason=f"the cited Passage of premise {item.id} is no longer "
                                       "stored")
        if source.kind == "pdf":
            chunks = deps.repos.passages.list(owner_id, ref.observation_id)
            entity = document_entity(source.title, [p.text for p in chunks])
            units += passage_units(text, [cited], deps.passage_token_cap,
                                   {ref.observation_id: entity})
            continue
        page = refetch.passages(source)
        if page is None:
            return ClaimVerdict(**base, verdict="unchecked",
                                reason=f"the Source of premise {item.id} could not be re-fetched")
        found, _ = _counterpart(deps, owner_id, run_id, section, index, cited, source, page)
        if found is not None:
            units += passage_units(text, [found], deps.passage_token_cap, {})
            continue
        best = [c.unit for c in _shown(
            screen(deps, item.statement, page, vectors).candidates, None)[:1]]
        units += best
    try:
        joint = judge_joint(deps, owner_id, run_id, text, units, section=section, claim=index)
    except Exception as e:  # noqa: BLE001 - fail closed
        return ClaimVerdict(**base, verdict="unverified",
                            reason=f"the support check failed ({type(e).__name__})")
    if joint.verdict != "SUPPORTED":
        return ClaimVerdict(**base, verdict="unverified",
                            reason=f"the premises together were judged {joint.verdict}")
    return ClaimVerdict(**base, verdict="verified", matched=units[0].ref)


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
        _joint_verdict(deps, refetch, vectors, owner_id, run_id, si, ci, c.text,
                       [items[e] for e in c.evidence_ids()]) if c.premises else
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
