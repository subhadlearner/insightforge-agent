"""SYNTHESIZING: Evidence items, ranked and held to a token budget.

Minimal form, no model call: each extracted statement becomes a candidate, is checked
against its Passage (provenance and literal values), de-duplicated by statement, given a
Confidence from its Sources and ranked. The entailment call, contradiction handling and As-of
derivation belong to the full synthesis ticket, so every `as_of_period` is UNKNOWN here."""

from urllib.parse import urlparse

from insightforge_agent.domain.contracts import (
    CONFIDENCE_ORDER,
    Confidence,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    ExtractionResult,
    PassageRef,
    ResearchResults,
)
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.ids import derive_evidence_id
from insightforge_agent.domain.models import Source
from insightforge_agent.domain.passages import numbers_in
from insightforge_agent.domain.plan import SubTask, SubTaskPlan
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.pipeline.deps import Deps

def _domain(source: Source) -> str:
    return (urlparse(source.locator).hostname or source.locator).lower().removeprefix("www.")


def _confidence(sources: list[Source], high_credibility: float) -> Confidence:
    # HIGH needs two Independent Sources. Different domains is checked here; whether one
    # cites the other needs the Source links, which come with the full ticket.
    if len({_domain(s) for s in sources}) >= 2:
        return "HIGH"
    best = max((s.credibility_score or 0.0) for s in sources)
    return "MEDIUM" if best >= high_credibility else "LOW"


def synthesize(
    deps: Deps, owner_id: str, run_id: str, plan: SubTaskPlan,
    research: ResearchResults, extraction: ExtractionResult,
) -> EvidenceBundle:
    aspects = {a.id: a.name for a in plan.aspects}
    by_id: dict[str, SubTask] = {t.id: t for t in plan.sub_tasks}
    owners: dict[str, SubTask] = {}  # source id -> the most important Sub-task that found it
    for result in sorted(research.results, key=lambda r: by_id[r.subtask_id].priority):
        if result.status != "succeeded":
            continue
        for ref in result.source_refs:
            owners.setdefault(ref.source_id, by_id[result.subtask_id])

    sources: dict[str, Source] = {}
    candidates: dict[str, dict] = {}  # evidence id -> {statement, subtask, supports, source ids}
    for fact in extraction.facts:
        subtask = owners.get(fact.source_id)
        if subtask is None:
            continue
        try:
            passage = deps.store.get_passages(owner_id, fact.passage.observation_id,
                                              [fact.passage.index])[0]
        except NotFoundError:
            deps.log(owner_id, run_id, "evidence_dropped", reason="no such Passage",
                     statement=fact.statement[:120])
            continue
        missing = numbers_in(fact.statement) - numbers_in(passage.text)
        if missing:
            deps.log(owner_id, run_id, "evidence_dropped", reason="value not in Passage",
                     values=sorted(missing), statement=fact.statement[:120])
            continue
        sources.setdefault(fact.source_id, deps.store.read_source(owner_id, fact.source_id).source)
        c = candidates.setdefault(
            derive_evidence_id(run_id, fact.statement),
            {"statement": fact.statement, "subtask": subtask, "supports": [], "sources": []},
        )
        if fact.passage not in c["supports"]:
            c["supports"].append(fact.passage)
            c["sources"].append(fact.source_id)

    def best_primary(c) -> PassageRef:
        ranked = sorted(
            zip(c["supports"], c["sources"], strict=True),
            key=lambda ps: (-(sources[ps[1]].credibility_score or 0.0),
                            ps[0].observation_id, ps[0].index),
        )
        return ranked[0][0]

    items: list[EvidenceItem] = []
    for eid, c in candidates.items():
        used = [sources[s] for s in dict.fromkeys(c["sources"])]
        items.append(EvidenceItem(
            id=eid, subtask_id=c["subtask"].id, aspect_id=c["subtask"].aspect_id,
            statement=c["statement"], confidence=_confidence(used, deps.high_credibility),
            supporting=c["supports"], primary=best_primary(c),
        ))

    def credibility(item: EvidenceItem) -> float:
        return max(sources[s].credibility_score or 0.0 for s in candidates[item.id]["sources"])

    items.sort(key=lambda i: (-credibility(i), by_id[i.subtask_id].priority, i.id))

    kept: list[EvidenceItem] = []
    used_tokens = 0
    for item in items:
        cost = estimate_tokens(item.statement)
        if kept and used_tokens + cost > deps.evidence_budget_tokens:
            # The full ticket compresses overflow; until then it is cut, and each cut logged.
            deps.log(owner_id, run_id, "evidence_cut", reason="over evidence budget",
                     evidence_id=item.id, confidence=item.confidence)
            continue
        kept.append(item)
        used_tokens += cost

    sections: list[EvidenceSection] = []
    for aspect_id, name in aspects.items():
        group = [i for i in kept if i.aspect_id == aspect_id]
        if group:
            label = min((i.confidence for i in group), key=CONFIDENCE_ORDER.__getitem__)
            sections.append(EvidenceSection(aspect_id=aspect_id, heading=name,
                                            confidence=label, items=group))
    return EvidenceBundle(sections=sections)
