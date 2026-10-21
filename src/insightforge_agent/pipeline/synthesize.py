"""SYNTHESIZING: verified, ranked Evidence items held to a token budget (design.md section 5,
ADR-0006).

1. Candidates: Extraction's facts are the only proposals (ADR-0006 §3); Synthesis no longer
   drafts its own. Each fact becomes a candidate of the Sub-task it serves: the one Extraction
   named, if that Sub-task read the fact's Passage, else the most important Sub-task that did.
2. Merge: candidates that say the same thing (same structured key) become one item. A period
   enters the key only if the cited Passage states it; with an UNKNOWN period the key also
   holds the Observations cited, so only candidates citing the same Observations merge.
3. Verify: each cited Passage must belong to this Run's Sources and exist (provenance), and
   state every figure of the statement (an integrity backstop: exact quantities, so "$71
   million" is "71 million dollars"). Then one support judgment per item (`pipeline/support`,
   chunked at the per-call cap) decides SUPPORTED, CONTRADICTED, INSUFFICIENT_EVIDENCE or
   VERIFICATION_ERROR for each Passage, with identity context only for Passages of an uploaded
   document whose own text establishes its company. Names are the judge's to decide: no lexical
   name rule refuses a candidate (the retired `literal_problem` stays only as the measured T18
   baseline). An item with no SUPPORTED Passage is dropped and logged: as verification
   incomplete (`DROP_UNVERIFIED`) when a Passage that could support it was never judged (every
   reply for it unusable), else as unsupported; with its Aspect and requirements, so the ledger
   tells "could not be checked" from "not found". A CONTRADICTED Passage marks the item conflicting only when the Extraction facts at
   that Passage share its entity, predicate and scope with an overlapping period.
4. Confidence counts SUPPORTED Passages only, over distinct Sources; two Sources whose
   supporting Passages are the same text (a syndicated story or a re-posted release) are not
   independent, whatever their domains.
5. Rank by the specified tuple, then fit the budget: first summarise lower-ranked items to
   their structured fact (same id and citation; logged `evidence_compressed`), then, only if
   still over, cut whole lowest-ranked items, never the last kept support of a required research
   requirement while anything else can go (logged `evidence_cut`)."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    ExtractedFact,
    ExtractionResult,
    PassageRef,
    PassageScope,
    ResearchResults,
)
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.evidence import (
    DROP_LITERAL,
    DROP_NO_CITATION,
    DROP_UNSUPPORTED,
    DROP_UNVERIFIED,
    compress_to_budget,
    confidence_of,
    credibility_bucket,
    decision_rule,
    rank_key,
    recency_bucket,
    section_label,
    sources_independent,
    summary_text,
    syndicated,
)
from insightforge_agent.domain.extraction import group_key, normalise, same_value
from insightforge_agent.domain.ids import derive_evidence_id
from insightforge_agent.domain.models import Passage, Source
from insightforge_agent.domain.passages import value_in_text
from insightforge_agent.domain.periods import (
    UNKNOWN,
    parse_period,
    period_stated_in,
    periods_overlap,
)
from insightforge_agent.domain.plan import SubTask, SubTaskPlan
from insightforge_agent.domain.provenance import (
    common_words,
    credited_by_document,
    document_entity,
    missing_names,
)
from insightforge_agent.domain.quantities import canonical_value, unsupported_numbers
from insightforge_agent.domain.requirements import RequirementLedger, aspect_ledger, links
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.embeddings import cosine
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.support import judge_units, passage_units
from insightforge_agent.pipeline.write import render_bundle

# Historical (T5-T18): the drafting prompt and reply schema Synthesis used before ADR-0006.
# Production no longer drafts; they are kept, unchanged, for the T18 B-2 harness and the F3
# live probe, which measured exactly this call.
DRAFT_SYSTEM = """You draft evidence items from numbered passages for one research sub-task.
Reply with JSON only:
{"items": [{"statement": "<one self-contained sentence, keeping every number and name exactly
as the passages write it>", "passages": [<numbers of every passage that bears on it, including any that disagrees>],
"entities": ["<named entity in the statement>"], "entity": "<the subject>", "predicate":
"<short snake_case relation>", "value": "<the stated value>", "scope": "<what it covers, or
empty>", "as_of_period": "<the period a passage states, as 2025, 2025-Q3, 2025-09 or
2025-01..2025-06, or UNKNOWN if none is stated>"}]}
Only state what the passages say. Leave entity, predicate and value empty if there is none.
When passages explicitly state management outlook or forward financial guidance relevant to the
research sub-task, draft it as a separate Evidence item with its stated period. Do not infer,
calculate, or invent guidance."""


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
    requirement_ids: list[str] = field(default_factory=list)


def merge_identity(
    *, statement: str, entity: str, predicate: str, value: str, scope: str, period: str,
    observations: Iterable[str],
) -> str:
    """What makes two candidates the same Evidence item (step 2, Merge). A candidate with an
    entity, predicate and value is keyed by its group key, stated period and canonical value;
    any other is keyed by its normalised statement. `period` is the already-normalised stated
    period (`stated_period`). The Evidence id is `derive_evidence_id(run_id, identity)`.

    With an UNKNOWN period nothing says two Observations describe the same moment (F4), so such a
    structured candidate is also keyed by the exact set of Observations it cites: it merges only
    with candidates citing the same ones."""
    structured = entity.strip() and predicate.strip() and value.strip()
    if structured:
        key = f"{group_key(entity, predicate, scope)}|{period}|{canonical_value(value) or normalise(value)}"
        if period == UNKNOWN:
            key += "|observations=" + ",".join(sorted(set(observations)))
        return key
    return normalise(statement)


def _key(ref: PassageRef) -> tuple[str, int]:
    return ref.observation_id, ref.index


def bundle_tokens(bundle: EvidenceBundle) -> int:
    """What the Writer will be shown, counted with the same estimate as the per-call cap."""
    return estimate_tokens(render_bundle(bundle))


def synthesize(
    deps: Deps, owner_id: str, run_id: str, brief: str, plan: SubTaskPlan,
    research: ResearchResults, extraction: ExtractionResult,
    ledger: RequirementLedger | None = None,
) -> EvidenceBundle:
    ledger = ledger or aspect_ledger(plan)
    by_id: dict[str, SubTask] = {t.id: t for t in plan.sub_tasks}
    sources_of: dict[str, list[str]] = {t.id: [] for t in plan.sub_tasks}
    # Per Sub-task: Source ID -> the exact Passages a Doc Reader retrieved (None: the whole Source).
    scope_of: dict[str, PassageScope] = {}
    for result in research.results:
        if result.status == "succeeded":
            # Deduplicated within a Sub-task only: a Source several Sub-tasks found is read for each.
            sources_of[result.subtask_id] = list(dict.fromkeys(r.source_id for r in result.source_refs))
            scope_of[result.subtask_id] = result.passage_scope()

    # The Run's own fetch of each Source: another Run may have fetched the same URL since.
    contents = {sid: deps.store.read_run_source(owner_id, run_id, sid)
                for sid in dict.fromkeys(s for ids in sources_of.values() for s in ids)}
    source_by_obs: dict[str, Source] = {
        c.observation.id: c.source for c in contents.values() if c.observation is not None}
    fetched_on: dict[str, date] = {
        c.observation.id: c.observation.fetched_at.date()
        for c in contents.values() if c.observation is not None}
    # Retrieved chunks name their own Observation, which need not be the Source's latest.
    for scope in scope_of.values():
        for sid, refs in scope.items():
            for obs_id in {r.observation_id for r in refs or ()} - source_by_obs.keys():
                obs = deps.repos.observations.get(owner_id, obs_id)
                source_by_obs[obs_id], fetched_on[obs_id] = contents[sid].source, obs.fetched_at.date()

    # 1. Candidates from Extraction's facts, each under a Sub-task that read its Passage.
    drafted = _candidates(deps, owner_id, run_id, plan, extraction, sources_of, scope_of)

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
        have.requirement_ids += [r for r in c.requirement_ids if r not in have.requirement_ids]

    # 3-4. Verify, then label with As-of period and Confidence.
    facts_at: dict[tuple[str, int], list[ExtractedFact]] = {}
    for f in extraction.facts:
        if f.passage is not None:
            facts_at.setdefault(_key(f.passage), []).append(f)
    facts_by_group: dict[tuple[str, str, str], list[ExtractedFact]] = {}
    for f in extraction.facts:
        if f.entity and f.predicate and f.value and f.passage is not None:
            facts_by_group.setdefault(group_key(f.entity, f.predicate, f.scope), []).append(f)
    texts_of: dict[str, list[str]] = {}  # source id -> its Passage text, to see who cites whom

    def source_texts(source: Source) -> list[str]:
        if source.id not in texts_of:
            texts_of[source.id] = [p.text for p in contents[source.id].passages]
        return texts_of[source.id]

    # Identity context (ADR-0006 §2): the company each uploaded document speaks for, when the
    # document's own text establishes it. Web Sources never have one.
    entity_of = {c.observation.id: document_entity(c.source.title, [p.text for p in c.passages])
                 for c in contents.values()
                 if c.observation is not None and c.source.kind == "pdf"}
    for obs_id, source in source_by_obs.items():
        if obs_id not in entity_of and source.kind == "pdf":
            entity_of[obs_id] = entity_of.get(contents[source.id].observation.id)
    brief_vector = deps.embedder.embed([brief])[0]
    items: list[EvidenceItem] = []
    scores: dict[str, float] = {}
    for c in merged.values():
        verified = _validate(deps, owner_id, run_id, c, source_by_obs, fetched_on, facts_at,
                             facts_by_group, entity_of)
        if verified is None:
            continue
        item, texts = verified
        supporting_sources = list(dict.fromkeys(source_by_obs[r.observation_id] for r in item.supporting))
        support_text = {s.id: [texts[_key(r)] for r in item.supporting
                               if source_by_obs[r.observation_id].id == s.id]
                        for s in supporting_sources}
        independent = any(
            sources_independent(a.locator, source_texts(a), b.locator, source_texts(b))
            and not syndicated(support_text[a.id], support_text[b.id])
            for i, a in enumerate(supporting_sources) for b in supporting_sources[i + 1:])
        best = max((s.credibility_score or 0.0) for s in supporting_sources)
        scores[item.id] = best
        decision = dict(has_independent_pair=independent, best_credibility=best,
                        conflicting=item.conflicting, high=deps.thresholds.credibility_high)
        item = item.model_copy(update={
            "confidence": confidence_of(**decision),
            "relevance": cosine(deps.embedder.embed([item.statement])[0], brief_vector),
        })
        # Logged before the budget cut, so an item that is later discarded stays explainable.
        deps.log(owner_id, run_id, "confidence_decided",
                 evidence_id=item.id, confidence=item.confidence,
                 decision_rule=decision_rule(**decision),
                 supported_passage_count=len(item.supporting),
                 distinct_supporting_source_count=len(supporting_sources),
                 independent_pair_found=independent, best_source_credibility=best,
                 credibility_high_threshold=deps.thresholds.credibility_high,
                 conflicting=item.conflicting,
                 source_ids=[s.id for s in supporting_sources])
        item = item.model_copy(update={"before_window": before_window(
            item, by_id[item.subtask_id].time_horizon_months, deps.now().date())})
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
    required = {r.id for r in ledger.requirements if r.priority == "required"}

    def build(chosen: list[EvidenceItem]) -> EvidenceBundle:
        sections = []
        for aspect_id, name in names.items():
            group = [i for i in chosen if i.aspect_id == aspect_id]
            if group:
                sections.append(EvidenceSection(
                    aspect_id=aspect_id, heading=name, items=group,
                    confidence=section_label([i.confidence for i in group])))
        return EvidenceBundle(sections=sections)

    def essential(item: EvidenceItem, kept: list[EvidenceItem]) -> list[str]:
        """The required requirements `item` is the last kept support of."""
        return [r for r in links(item, ledger) if r in required and not any(
            r in links(other, ledger) for other in kept if other is not item)]

    kept, cut, short = compress_to_budget(
        ranked, lambda chosen: bundle_tokens(build(chosen)) <= deps.evidence_budget_tokens,
        lambda item, chosen: bool(essential(item, chosen)))
    rank = {i.id: n for n, i in enumerate(ranked, start=1)}
    for item in short:
        deps.log(owner_id, run_id, "evidence_compressed",
                 reason="over evidence budget: shown to the Writer as its structured fact",
                 evidence_id=item.id, subtask_id=item.subtask_id, confidence=item.confidence,
                 rank=rank[item.id], statement_tokens=estimate_tokens(item.statement),
                 shown_tokens=estimate_tokens(summary_text(item)))
    for item in cut:
        deps.log(owner_id, run_id, "evidence_cut", reason="over evidence budget",
                 evidence_id=item.id, subtask_id=item.subtask_id, confidence=item.confidence,
                 rank=rank[item.id], requirement_ids=links(item, ledger),
                 last_support_of=essential(item, [*kept, item]))
    _log_recovery_outcome(deps, owner_id, run_id, research, items, kept)
    return build(kept)


def _candidates(deps: Deps, owner_id: str, run_id: str, plan: SubTaskPlan,
                extraction: ExtractionResult, sources_of: dict[str, list[str]],
                scope_of: dict[str, PassageScope]) -> list[_Candidate]:
    """Step 1: one candidate per extracted fact a Sub-task read. A fact whose Passage no
    succeeded Sub-task read (or whose provenance is gone) is logged, never guessed at."""
    tasks = sorted(plan.sub_tasks, key=lambda t: (t.priority, t.id))
    out = []
    for f in extraction.facts:
        if f.passage is None or f.source_id is None:
            continue
        readers = [t for t in tasks if f.source_id in sources_of.get(t.id, [])
                   and (scope_of[t.id].get(f.source_id) is None
                        or f.passage in scope_of[t.id][f.source_id])]
        if not readers:
            deps.log(owner_id, run_id, "evidence_dropped", reason=DROP_NO_CITATION,
                     fact_id=f.id, statement=f.statement[:120])
            continue
        task = next((t for t in readers if t.id == f.subtask_id), readers[0])
        identity = merge_identity(statement=f.statement, entity=f.entity, predicate=f.predicate,
                                  value=f.value, scope=f.scope, period=f.period,
                                  observations=[f.passage.observation_id])
        out.append(_Candidate(
            id=derive_evidence_id(run_id, identity), statement=f.statement, subtask=task,
            refs=[f.passage], entities=list(f.entities), entity=f.entity,
            predicate=f.predicate, value=f.value, scope=f.scope, period=f.period,
            requirement_ids=list(f.requirement_ids)))
    return out


def before_window(item: EvidenceItem, horizon_months: int, today: date) -> bool:
    """T18 CS4: an item with no stated period whose Source was published more than the
    Sub-task's time horizon before today may predate the window it asked for. Never set for a
    stated (or derived) period, and never a claim that the fact is outdated."""
    p = item.published_at
    if item.as_of_period != UNKNOWN or p is None:
        return False
    months = (today.year - p.year) * 12 + today.month - p.month - (today.day < p.day)
    return months > horizon_months


def _log_recovery_outcome(deps: Deps, owner_id: str, run_id: str, research: ResearchResults,
                          validated: list[EvidenceItem], retained: list[EvidenceItem]) -> None:
    """T18 CS2: for each recovered documents Sub-task, what recovery alone contributed: its
    chunks no Document Reader selected, the items citing one that passed provenance, figures
    and support, and those kept in the bundle. Retrieval alone never counts."""
    selected = {(p.observation_id, p.index) for r in research.results
                if r.status == "succeeded" and r.scope_origin == "doc_reader"
                for ref in r.source_refs for p in ref.passages}
    for r in research.results:
        if r.scope_origin != "search_recovery":
            continue
        chunks = {(p.observation_id, p.index) for ref in r.source_refs for p in ref.passages}
        only = chunks - selected

        def citing(items: list[EvidenceItem]) -> int:
            return sum(1 for i in items if any(_key(s) in only for s in i.supporting))

        deps.log(owner_id, run_id, "document_recovery_outcome", subtask_id=r.subtask_id,
                 recovered_chunks=len(chunks), recovered_only_chunks=len(only),
                 validated_items=citing(validated), retained_items=citing(retained))


def literal_problem(
    *, statement: str, entities: list[str], entity: str, passage_text: str,
    common: frozenset[str] | None = None, document_entity: str | None = None,
) -> str | None:
    """Retired from production by ADR-0006. The T5-T18 lexical gate, kept unchanged as the
    deterministic baseline the ADR-0005 benchmark and the historical T18 replays measure.

    Every number and every name the statement asserts must appear in the Passage
    (`domain/provenance`, T18 CS1B: a model-written entity label counts only through the names
    the statement itself uses). Returns the reason it does not, or None. Lexical only: it says
    nothing about whether the Passage supports the statement. `common` is the lower-case
    vocabulary of the Passages the Run read; without it, that of this Passage and statement.
    `document_entity` (uploaded documents only, T18 CS2): the company the document speaks for,
    accepted in a chunk of that document that names no one else."""
    missing_numbers = unsupported_numbers(statement, passage_text)
    if missing_numbers:
        return f"value not in Passage: {sorted(missing_numbers)}"
    vocabulary = common if common is not None else common_words([passage_text, statement])
    names = missing_names(statement=statement, entities=entities, entity=entity,
                          passage_text=passage_text, common=vocabulary).missing
    if names and credited_by_document(names, entity=document_entity, statement=statement,
                                      chunk_text=passage_text, common=vocabulary):
        names = []
    if names:
        return f"name not in Passage: {names}"
    return None


def _validate(
    deps: Deps, owner_id: str, run_id: str, c: _Candidate, source_by_obs: dict[str, Source],
    fetched_on: dict[str, date], facts_at: dict[tuple[str, int], list[ExtractedFact]],
    facts_by_group: dict[tuple[str, str, str], list[ExtractedFact]],
    entity_of: dict[str, str | None] | None = None,
) -> tuple[EvidenceItem, dict[tuple[str, int], str]] | None:
    """Step 3. The verified item and the text of every Passage judged, or None (dropped)."""
    entity_of = entity_of or {}

    def drop(reason: str, **extra) -> None:
        deps.log(owner_id, run_id, "evidence_dropped", reason=reason, evidence_id=c.id,
                 subtask_id=c.subtask.id, statement=c.statement[:120], **extra)

    # Deterministic invariants first: provenance gates every cited Passage, and only a Passage
    # stating every figure may support the item. A Passage that fails the figure check may still
    # contradict it, so it is still judged.
    candidates: list[Passage] = []
    can_support: set[tuple[str, int]] = set()
    refs: list[dict] = []  # each cited Passage with why it cannot support: replayable offline

    for ref in c.refs:
        if ref.observation_id not in source_by_obs:
            refs.append({**ref.model_dump(), "reason": "not from this Run's Sources"})
            continue
        try:
            passage = deps.store.get_passages(owner_id, ref.observation_id, [ref.index])[0]
        except NotFoundError:
            refs.append({**ref.model_dump(), "reason": "no such Passage"})
            continue
        candidates.append(passage)
        missing = unsupported_numbers(c.statement, passage.text)
        if missing:
            refs.append({**ref.model_dump(), "reason": f"value not in Passage: {sorted(missing)}"})
        else:
            can_support.add(_key(ref))
    if not can_support:
        drop(DROP_LITERAL, rejected=[r["reason"] for r in refs], refs=refs)
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

    # Passages that disagree may come from another Source. The Extraction facts say where:
    # same entity, predicate and scope, a different value, and not a clearly different period.
    # They are judged like any other candidate, but can never support the item.
    if c.entity and c.predicate and value:
        for f in facts_by_group.get(group_key(c.entity, c.predicate, c.scope), []):
            k = _key(f.passage)
            if (k in text_of or f.passage.observation_id not in source_by_obs
                    or same_value(f.value, value)
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

    # The support judgment (ADR-0006 §1), chunked at the per-call cap. A Passage judged in
    # several windows supports the item if any window does; any window contradicting it counts.
    judgments = judge_units(deps, owner_id, run_id, c.statement,
                            passage_units(c.statement, candidates, deps.passage_token_cap,
                                          entity_of),
                            evidence_id=c.id, subtask_id=c.subtask.id)
    verdicts: dict[tuple[str, int], set[str]] = {}
    present_state: set[tuple[str, int]] = set()
    for j in judgments:
        verdicts.setdefault(_key(j.ref), set()).add(j.verdict)
        if j.present_state:
            present_state.add(_key(j.ref))
    supported = [PassageRef(observation_id=o, index=i) for (o, i), vs in verdicts.items()
                 if "SUPPORTED" in vs and (o, i) in can_support]
    contradicted = [PassageRef(observation_id=o, index=i) for (o, i), vs in verdicts.items()
                    if "CONTRADICTED" in vs and not ("SUPPORTED" in vs and (o, i) in can_support)]
    errors = sum(1 for vs in verdicts.values() if vs == {"VERIFICATION_ERROR"})
    unjudged = [k for k in can_support if verdicts.get(k, set()) <= {"VERIFICATION_ERROR"}]
    outcome = "supported" if supported else DROP_UNVERIFIED if unjudged else DROP_UNSUPPORTED
    # Per-Passage outcome: references and verdicts only, never Passage text or replies.
    deps.log(owner_id, run_id, "evidence_support", evidence_id=c.id, subtask_id=c.subtask.id,
             passages=[{"observation_id": k[0], "index": k[1], "verdicts": sorted(verdicts.get(k, ())),
                        "figure_gate": k in can_support,
                        "identity_context": bool(entity_of.get(k[0]))}
                       for k in (_key(p) for p in candidates)],
             outcome=outcome)
    if not supported:
        drop(outcome, verdicts={"contradicted": len(contradicted)},
             verification_errors=errors, unjudged=len(unjudged), aspect_id=c.subtask.aspect_id,
             requirement_ids=list(c.requirement_ids))
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
            and not same_value(f.value, value)
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
    published = source_by_obs[primary.observation_id].published_at
    return EvidenceItem(
        id=c.id, subtask_id=c.subtask.id, aspect_id=c.subtask.aspect_id, statement=c.statement,
        published_at=published.date() if published else None,
        confidence="LOW", supporting=supported, primary=primary, entity=c.entity,
        predicate=c.predicate, scope=c.scope, value=value, as_of_period=period,
        derived_from=derived, temporally_ambiguous=ambiguous, conflicting=conflicting,
        contradicting=contradicting, conflict_fact_ids=linked,
        requirement_ids=list(c.requirement_ids),
    ), text_of
