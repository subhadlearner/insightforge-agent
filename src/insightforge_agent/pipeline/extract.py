"""EXTRACTING: entities and structured facts from one Source at a time (ADR-0002).

One bounded model call per Passage batch. Each Source's facts are stored as they are
extracted. Conflicts are then detected over the structured facts grouped by entity, never
over raw text from several Sources at once."""

from pydantic import BaseModel, Field

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import ExtractedFact, ExtractionResult, PassageRef
from insightforge_agent.domain.extraction import detect_conflicts
from insightforge_agent.domain.ids import derive_fact_id
from insightforge_agent.domain.passages import batch_passages
from insightforge_agent.domain.periods import UNKNOWN, parse_period
from insightforge_agent.pipeline.deps import Deps

MAX_STATEMENT_CHARS = 600  # a statement is one sentence; a pasted page is not

SYSTEM = """You extract facts from numbered passages of one web page. Reply with JSON only:
{"facts": [{"passage_index": <int>, "statement": "<one self-contained sentence the passage
states, keeping every number and name exactly as written>", "entities": ["<named entity>"],
"entity": "<the subject the fact is about>", "predicate": "<short snake_case relation, such as
units_sold or starting_price>", "value": "<the stated value, exactly as written>",
"scope": "<what it covers, such as global or China, or empty>", "period": "<the period the
passage states, as 2025, 2025-Q3, 2025-09 or 2025-01..2025-06, or UNKNOWN if it states none>"}]}
Only include facts a passage states. Use at most 3 facts per passage. Leave entity, predicate
and value empty when a fact has no such structure."""


class _Fact(BaseModel):
    passage_index: int
    statement: str
    entities: list[str] = Field(default_factory=list)
    entity: str = ""
    predicate: str = ""
    value: str = ""
    scope: str = ""
    period: str = UNKNOWN


class _Facts(BaseModel):
    facts: list[_Fact] = Field(default_factory=list)


def extract(deps: Deps, owner_id: str, run_id: str, source_ids: list[str]) -> ExtractionResult:
    facts: list[ExtractedFact] = []
    entities: dict[str, None] = {}
    for sid in source_ids:
        contents = deps.store.read_source(owner_id, sid)
        if contents.observation is None:
            continue
        obs_id = contents.observation.id
        from_source: dict[str, ExtractedFact] = {}
        for batch in batch_passages(contents.passages, deps.passage_token_cap):
            known = {p.index for p in batch}
            numbered = "\n\n".join(f"[{p.index}] {p.text}" for p in batch)
            user = f"Page title: {contents.source.title}\n\n{numbered}"
            deps.spend(owner_id, run_id, SYSTEM, user)
            try:
                reply = ask_json(deps.light_model, SYSTEM, user, _Facts)
            except BadModelReply as e:
                deps.log(owner_id, run_id, "extraction_batch_failed", source_id=sid, detail=str(e))
                continue
            for f in reply.facts:
                if (f.passage_index not in known or not f.statement.strip()
                        or len(f.statement) > MAX_STATEMENT_CHARS):
                    deps.log(owner_id, run_id, "extraction_fact_dropped", source_id=sid,
                             passage_index=f.passage_index)
                    continue
                statement = f.statement.strip()
                fact = ExtractedFact(
                    id=derive_fact_id(sid, obs_id, f.passage_index, statement),
                    source_id=sid, passage=PassageRef(observation_id=obs_id, index=f.passage_index),
                    statement=statement, entities=f.entities, entity=f.entity.strip(),
                    predicate=f.predicate.strip(), value=f.value.strip(), scope=f.scope.strip(),
                    period=f.period.strip() if parse_period(f.period) else UNKNOWN,
                )
                from_source.setdefault(fact.id, fact)
        deps.repos.entities.upsert_facts(owner_id, run_id, list(from_source.values()))
        facts.extend(from_source.values())
        for fact in from_source.values():
            entities.update(dict.fromkeys([*fact.entities, *([fact.entity] if fact.entity else [])]))

    facts, conflicts = detect_conflicts(facts)
    if conflicts:
        deps.repos.entities.upsert_facts(owner_id, run_id, [f for f in facts if f.conflict])
        for c in conflicts:
            deps.log(owner_id, run_id, "extraction_conflict", entity=c.entity,
                     predicate=c.predicate, scope=c.scope, fact_ids=c.fact_ids)
    return ExtractionResult(entities=list(entities), facts=facts, conflicts=conflicts)
