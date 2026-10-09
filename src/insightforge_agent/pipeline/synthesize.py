"""SYNTHESIZING: validated, ranked Evidence items held to a token budget (design.md section 5).

1. Draft: per Sub-task, over that Sub-task's Passages in bounded batches, a model call drafts
   candidate Evidence items (statement, supporting Passages, entity, predicate, value, scope
   and the As-of period the Passage states).
2. Merge: candidates that say the same thing (same structured key) become one item.
3. Validate: each Passage must pass a deterministic provenance check and a literal-value
   check; then one entailment call per item (chunked at the per-call cap) judges the rest
   SUPPORTED, CONTRADICTED or INSUFFICIENT_EVIDENCE. An item with no SUPPORTED Passage is
   dropped and logged. A CONTRADICTED Passage marks the item conflicting only when the
   Extraction facts at that Passage share its entity, predicate and scope with an overlapping
   period.
4. Confidence counts SUPPORTED Passages only, over distinct Sources.
5. Rank by the specified tuple, then cut whole lowest-ranked items to fit the budget."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    ExtractedFact,
    ExtractionResult,
    PassageRef,
    ResearchResults,
)
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.evidence import (
    cites,
    compress_to_budget,
    confidence_of,
    credibility_bucket,
    domain_of,
    rank_key,
    recency_bucket,
    section_label,
)
from insightforge_agent.domain.extraction import group_key, normalise
from insightforge_agent.domain.ids import derive_evidence_id
from insightforge_agent.domain.models import Passage, Source
from insightforge_agent.domain.passages import (
    Window,
    batch_passages,
    named_entities_in,
    numbers_in,
    value_in_text,
)
from insightforge_agent.domain.periods import (
    UNKNOWN,
    parse_period,
    period_stated_in,
    periods_overlap,
    stated_period,
)
from insightforge_agent.domain.plan import SubTask, SubTaskPlan
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.embeddings import cosine
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.write import render_bundle

Verdict = Literal["SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]

DRAFT_SYSTEM = """You draft evidence items from numbered passages for one research sub-task.
Reply with JSON only:
{"items": [{"statement": "<one self-contained sentence, keeping every number and name exactly
as the passages write it>", "passages": [<numbers of every passage that bears on it, including any that disagrees>],
"entities": ["<named entity in the statement>"], "entity": "<the subject>", "predicate":
"<short snake_case relation>", "value": "<the stated value>", "scope": "<what it covers, or
empty>", "as_of_period": "<the period a passage states, as 2025, 2025-Q3, 2025-09 or
2025-01..2025-06, or UNKNOWN if none is stated>"}]}
Only state what the passages say. Leave entity, predicate and value empty if there is none."""

ENTAIL_SYSTEM = """You judge whether numbered passages support one evidence statement.
Reply with JSON only:
{"verdicts": [{"passage": <number>, "verdict": "SUPPORTED" | "CONTRADICTED" |
"INSUFFICIENT_EVIDENCE", "asserts_present_state": <true if the passage asserts how things
stand now, not history, a prediction or an announcement>}]}
SUPPORTED: the passage states the statement. CONTRADICTED: it states something that
conflicts with it. INSUFFICIENT_EVIDENCE: neither. Give one verdict for every passage."""


class _DraftItem(BaseModel):
    statement: str
    passages: list[int] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    entity: str = ""
    predicate: str = ""
    value: str = ""
    scope: str = ""
    as_of_period: str = UNKNOWN


class _Drafts(BaseModel):
    items: list[_DraftItem] = Field(default_factory=list)


class _Verdict(BaseModel):
    passage: int
    verdict: Verdict = "INSUFFICIENT_EVIDENCE"
    asserts_present_state: bool = False


class _Verdicts(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


@dataclass
class _Candidate:
    id: str
    statement: str
    subtask: SubTask
    refs: list[PassageRef]
    entities: list[str]
    entity: str
    predicate: str
    value: str
    scope: str
    period: str  # as the Passage states it, or UNKNOWN


def _key(ref: PassageRef) -> tuple[str, int]:
    return ref.observation_id, ref.index


def bundle_tokens(bundle: EvidenceBundle) -> int:
    """What the Writer will be shown, counted with the same estimate as the per-call cap."""
    return estimate_tokens(render_bundle(bundle))


def synthesize(
    deps: Deps, owner_id: str, run_id: str, brief: str, plan: SubTaskPlan,
    research: ResearchResults, extraction: ExtractionResult,
) -> EvidenceBundle:
    by_id: dict[str, SubTask] = {t.id: t for t in plan.sub_tasks}
    sources_of: dict[str, list[str]] = {t.id: [] for t in plan.sub_tasks}
    for result in research.results:
        if result.status == "succeeded":
            # Deduplicated within a Sub-task only: a Source several Sub-tasks found is read for each.
            sources_of[result.subtask_id] = list(dict.fromkeys(r.source_id for r in result.source_refs))

    contents = {sid: deps.store.read_source(owner_id, sid)
                for sid in dict.fromkeys(s for ids in sources_of.values() for s in ids)}
    source_by_obs: dict[str, Source] = {
        c.observation.id: c.source for c in contents.values() if c.observation is not None}
    fetched_on: dict[str, date] = {
        c.observation.id: c.observation.fetched_at.date()
        for c in contents.values() if c.observation is not None}

    # 1. Draft, one Sub-task at a time.
    drafted: list[_Candidate] = []
    for task in sorted(plan.sub_tasks, key=lambda t: (t.priority, t.id)):
        passages = [p for sid in sources_of[task.id] for p in contents[sid].passages]
        for batch in batch_passages(passages, deps.passage_token_cap):
            drafted += _draft(deps, owner_id, run_id, task, batch)

    # 2. Merge candidates that say the same thing; the most important Sub-task keeps the item.
    merged: dict[str, _Candidate] = {}
    for c in drafted:
        have = merged.get(c.id)
        if have is None:
            merged[c.id] = c
            continue
        if c.subtask.priority < have.subtask.priority:
            have.subtask = c.subtask
        have.refs += [r for r in c.refs if r not in have.refs]
        have.entities += [e for e in c.entities if e not in have.entities]

    # 3-4. Validate, then label with As-of period and Confidence.
    facts_at: dict[tuple[str, int], list[ExtractedFact]] = {}
    for f in extraction.facts:
        facts_at.setdefault(_key(f.passage), []).append(f)
    facts_by_group: dict[tuple[str, str, str], list[ExtractedFact]] = {}
    for f in extraction.facts:
        if f.entity and f.predicate and f.value:
            facts_by_group.setdefault(group_key(f.entity, f.predicate, f.scope), []).append(f)
    texts_of: dict[str, list[str]] = {}  # source id -> its Passage text, to see who cites whom

    def source_texts(source: Source) -> list[str]:
        if source.id not in texts_of:
            texts_of[source.id] = [p.text for p in contents[source.id].passages]
        return texts_of[source.id]

    brief_vector = deps.embedder.embed([brief])[0]
    items: list[EvidenceItem] = []
    scores: dict[str, float] = {}
    for c in merged.values():
        item = _validate(deps, owner_id, run_id, c, source_by_obs, fetched_on, facts_at,
                         facts_by_group)
        if item is None:
            continue
        supporting_sources = list(dict.fromkeys(source_by_obs[r.observation_id] for r in item.supporting))
        independent = any(
            domain_of(a.locator) != domain_of(b.locator)
            and not cites(source_texts(a), b.locator) and not cites(source_texts(b), a.locator)
            for i, a in enumerate(supporting_sources) for b in supporting_sources[i + 1:])
        best = max((s.credibility_score or 0.0) for s in supporting_sources)
        scores[item.id] = best
        item = item.model_copy(update={
            "confidence": confidence_of(
                has_independent_pair=independent, best_credibility=best,
                conflicting=item.conflicting, high=deps.thresholds.credibility_high),
            "relevance": cosine(deps.embedder.embed([item.statement])[0], brief_vector),
        })
        items.append(item)

    # 5. Rank, then 6. fit to the budget.
    today = deps.now().date()
    priority = {i.id: by_id[i.subtask_id].priority for i in items}

    def key(i: EvidenceItem):
        return rank_key(
            i, recency=recency_bucket(i.as_of_period, today, deps.thresholds),
            credibility=credibility_bucket(scores[i.id], deps.thresholds), priority=priority[i.id])

    ranked = sorted(items, key=key)
    names = {a.id: a.name for a in plan.aspects}

    def build(chosen: list[EvidenceItem]) -> EvidenceBundle:
        sections = []
        for aspect_id, name in names.items():
            group = [i for i in chosen if i.aspect_id == aspect_id]
            if group:
                sections.append(EvidenceSection(
                    aspect_id=aspect_id, heading=name, items=group,
                    confidence=section_label([i.confidence for i in group])))
        return EvidenceBundle(sections=sections)

    kept, cut = compress_to_budget(
        ranked, lambda chosen: bundle_tokens(build(chosen)) <= deps.evidence_budget_tokens)
    for item in cut:
        deps.log(owner_id, run_id, "evidence_cut", reason="over evidence budget",
                 evidence_id=item.id, confidence=item.confidence, rank=ranked.index(item) + 1)
    return build(kept)


def _draft(deps: Deps, owner_id: str, run_id: str, task: SubTask, batch: list[Window]) -> list[_Candidate]:
    refs = {n: PassageRef(observation_id=w.passage.observation_id, index=w.passage.index)
            for n, w in enumerate(batch, start=1)}
    numbered = "\n\n".join(f"[{n}] {w.shown}" for n, w in enumerate(batch, start=1))
    user = f"Sub-task: {task.query}\n\n{numbered}"
    deps.spend(owner_id, run_id, DRAFT_SYSTEM, user)
    try:
        reply = ask_json(deps.light_model, DRAFT_SYSTEM, user, _Drafts)
    except BadModelReply as e:
        deps.log(owner_id, run_id, "draft_batch_failed", subtask_id=task.id, detail=str(e))
        return []
    out: list[_Candidate] = []
    for d in reply.items:
        statement = d.statement.strip()
        cited = list(dict.fromkeys(refs[n] for n in d.passages if n in refs))
        if not statement or not cited:
            deps.log(owner_id, run_id, "evidence_dropped", reason="draft cites no Passage",
                     statement=statement[:120])
            continue
        period = stated_period(d.as_of_period)
        structured = d.entity.strip() and d.predicate.strip() and d.value.strip()
        identity = (f"{group_key(d.entity, d.predicate, d.scope)}|{period}|{normalise(d.value)}"
                    if structured else normalise(statement))
        out.append(_Candidate(
            id=derive_evidence_id(run_id, identity), statement=statement, subtask=task,
            refs=cited, entities=[e.strip() for e in d.entities if e.strip()],
            entity=d.entity.strip(), predicate=d.predicate.strip(), value=d.value.strip(),
            scope=d.scope.strip(), period=period))
    return out


def _literal_problem(c: _Candidate, passage: Passage) -> str | None:
    """Every number and named entity of the item must appear in the Passage."""
    text = passage.text.casefold()
    missing_numbers = numbers_in(c.statement) - numbers_in(passage.text)
    if missing_numbers:
        return f"value not in Passage: {sorted(missing_numbers)}"
    names = {*c.entities, *([c.entity] if c.entity else []), *named_entities_in(c.statement)}
    missing_names = sorted(n for n in names if n.casefold() not in text)
    if missing_names:
        return f"name not in Passage: {missing_names}"
    return None


def _validate(
    deps: Deps, owner_id: str, run_id: str, c: _Candidate, source_by_obs: dict[str, Source],
    fetched_on: dict[str, date], facts_at: dict[tuple[str, int], list[ExtractedFact]],
    facts_by_group: dict[tuple[str, str, str], list[ExtractedFact]],
) -> EvidenceItem | None:
    def drop(reason: str, **extra) -> None:
        deps.log(owner_id, run_id, "evidence_dropped", reason=reason, evidence_id=c.id,
                 statement=c.statement[:120], **extra)

    # Deterministic checks first. Provenance gates every candidate. The literal-value check
    # decides which candidates may support the item; a Passage that fails it may still
    # contradict it, so it still goes to the entailment call.
    candidates: list[Passage] = []
    can_support: set[tuple[str, int]] = set()
    rejected: list[str] = []
    for ref in c.refs:
        if ref.observation_id not in source_by_obs:
            rejected.append("not from this Run's Sources")
            continue
        try:
            passage = deps.store.get_passages(owner_id, ref.observation_id, [ref.index])[0]
        except NotFoundError:
            rejected.append("no such Passage")
            continue
        candidates.append(passage)
        problem = _literal_problem(c, passage)
        if problem:
            rejected.append(problem)
        else:
            can_support.add(_key(ref))
    if not can_support:
        drop("no Passage passes the provenance and literal-value checks", rejected=rejected)
        return None
    text_of = {(p.observation_id, p.index): p.text for p in candidates}

    # The structured value and the stated period are model output too: neither may decide a
    # conflict or a ranking unless a Passage that can support the item actually states it.
    value = c.value
    if value and not any(value_in_text(value, text_of[k]) for k in can_support):
        deps.log(owner_id, run_id, "value_rejected", evidence_id=c.id, value=value)
        value = ""
    period = c.period
    if period != UNKNOWN and not any(period_stated_in(period, text_of[k]) for k in can_support):
        deps.log(owner_id, run_id, "period_rejected", evidence_id=c.id, period=period)
        period = UNKNOWN

    # Passages that disagree may sit in another drafting batch. The Extraction facts say where:
    # same entity, predicate and scope, a different value, and not a clearly different period.
    # They go to the entailment call like any other candidate, but can never support the item.
    if c.entity and c.predicate and value:
        for f in facts_by_group.get(group_key(c.entity, c.predicate, c.scope), []):
            k = _key(f.passage)
            if (k in text_of or f.passage.observation_id not in source_by_obs
                    or normalise(f.value) == normalise(value)
                    or (parse_period(period) and parse_period(f.period)
                        and not periods_overlap(period, f.period))):
                continue
            try:
                challenger = deps.store.get_passages(owner_id, f.passage.observation_id,
                                                     [f.passage.index])[0]
            except NotFoundError:
                continue
            candidates.append(challenger)
            text_of[k] = challenger.text

    # One bounded entailment call per item, chunked when the candidates exceed the cap. A
    # Passage over the cap is read in windows; its verdicts are combined per Passage.
    verdicts: dict[tuple[str, int], set[str]] = {}
    present_state: set[tuple[str, int]] = set()
    for batch in batch_passages(candidates, deps.passage_token_cap):
        numbered = "\n\n".join(f"[{n}] {w.shown}" for n, w in enumerate(batch, start=1))
        user = f"Statement: {c.statement}\n\n{numbered}"
        deps.spend(owner_id, run_id, ENTAIL_SYSTEM, user)
        try:
            reply = ask_json(deps.light_model, ENTAIL_SYSTEM, user, _Verdicts)
        except BadModelReply as e:
            deps.log(owner_id, run_id, "entailment_failed", evidence_id=c.id, detail=str(e))
            continue
        for v in reply.verdicts:
            if not 1 <= v.passage <= len(batch):
                continue
            k = (batch[v.passage - 1].passage.observation_id, batch[v.passage - 1].passage.index)
            verdicts.setdefault(k, set()).add(v.verdict)
            if v.verdict == "SUPPORTED" and v.asserts_present_state:
                present_state.add(k)
    supported = [PassageRef(observation_id=o, index=i) for (o, i), vs in verdicts.items()
                 if "SUPPORTED" in vs and (o, i) in can_support]
    contradicted = [PassageRef(observation_id=o, index=i) for (o, i), vs in verdicts.items()
                    if "CONTRADICTED" in vs and not ("SUPPORTED" in vs and (o, i) in can_support)]
    if not supported:
        drop("no supported Passage", verdicts={"contradicted": len(contradicted)})
        return None
    if period != UNKNOWN and not any(period_stated_in(period, text_of[_key(r)]) for r in supported):
        deps.log(owner_id, run_id, "period_rejected", evidence_id=c.id, period=period)
        period = UNKNOWN

    # Primary Passage: highest Source credibility, then relevance to the item, then lowest id.
    vectors = deps.embedder.embed([c.statement] + [text_of[_key(r)] for r in supported])
    similarity = {_key(r): cosine(vectors[0], v) for r, v in zip(supported, vectors[1:], strict=True)}
    primary = min(supported, key=lambda r: (
        -(source_by_obs[r.observation_id].credibility_score or 0.0),
        -round(similarity[_key(r)], 6), r.observation_id, r.index))

    derived, ambiguous = None, False
    if parse_period(period) is None:
        # Nothing stated: derive from the Observation date only for a present-state assertion
        # from a Source with no publication date; otherwise the period is unknown.
        basis = next((r for r in [primary, *supported]
                      if _key(r) in present_state
                      and source_by_obs[r.observation_id].published_at is None), None)
        if basis is not None:
            period, derived = fetched_on[basis.observation_id].isoformat(), "observation_date"
            deps.log(owner_id, run_id, "as_of_derived", evidence_id=c.id, as_of_period=period,
                     derived_from=derived)
        else:
            period, ambiguous = UNKNOWN, True

    # Contradictions: a genuine one needs the same entity, predicate and scope, a different
    # structured value and an overlapping period. A CONTRADICTED verdict alone is not enough:
    # anything else is a distinct fact (or a mistaken verdict) and is left out.
    mine = group_key(c.entity, c.predicate, c.scope)
    conflicting, contradicting, linked = False, [], []
    for ref in contradicted:
        genuine = [
            f for f in facts_at.get(_key(ref), [])
            if value and c.entity and c.predicate and f.value
            and group_key(f.entity, f.predicate, f.scope) == mine
            and normalise(f.value) != normalise(value)
            and periods_overlap(period, f.period)]
        if genuine:
            conflicting = True
            contradicting.append(ref)
            linked += [f.id for f in genuine if f.id not in linked]
        else:
            deps.log(owner_id, run_id, "contradiction_ignored", evidence_id=c.id,
                     passage=ref.model_dump(),
                     reason="same value, or a different entity, predicate, scope or period")
    if conflicting:
        for ref in supported:
            linked += [f.id for f in facts_at.get(_key(ref), [])
                       if f.entity and group_key(f.entity, f.predicate, f.scope) == mine
                       and f.id not in linked]
    return EvidenceItem(
        id=c.id, subtask_id=c.subtask.id, aspect_id=c.subtask.aspect_id, statement=c.statement,
        confidence="LOW", supporting=supported, primary=primary, entity=c.entity,
        predicate=c.predicate, scope=c.scope, value=value, as_of_period=period,
        derived_from=derived, temporally_ambiguous=ambiguous, conflicting=conflicting,
        contradicting=contradicting, conflict_fact_ids=linked,
    )
