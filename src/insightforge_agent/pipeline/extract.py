"""EXTRACTING: entities and statements from one Source at a time (ADR-0002).

Minimal form: one bounded model call per Passage batch. Conflict detection over the
structured facts comes with the full Extraction ticket."""

from pydantic import BaseModel, Field

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import ExtractedFact, ExtractionResult, PassageRef
from insightforge_agent.domain.passages import batch_passages
from insightforge_agent.pipeline.deps import Deps

MAX_STATEMENT_CHARS = 600  # a statement is one sentence; a pasted page is not

SYSTEM = """You extract facts from numbered passages of one web page. Reply with JSON only:
{"facts": [{"passage_index": <int>, "statement": "<one self-contained sentence the passage
states, keeping every number and name exactly as written>", "entities": ["<named entity>"]}]}
Only include facts a passage states. Use at most 3 facts per passage."""


class _Fact(BaseModel):
    passage_index: int
    statement: str
    entities: list[str] = Field(default_factory=list)


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
        for batch in batch_passages(contents.passages, deps.passage_token_cap):
            known = {p.index for p in batch}
            numbered = "\n\n".join(f"[{p.index}] {p.text}" for p in batch)
            try:
                reply = ask_json(deps.light_model, SYSTEM,
                                 f"Page title: {contents.source.title}\n\n{numbered}", _Facts)
            except BadModelReply as e:
                deps.log(owner_id, run_id, "extraction_batch_failed", source_id=sid, detail=str(e))
                continue
            for f in reply.facts:
                if (f.passage_index not in known or not f.statement.strip()
                        or len(f.statement) > MAX_STATEMENT_CHARS):
                    deps.log(owner_id, run_id, "extraction_fact_dropped", source_id=sid,
                             passage_index=f.passage_index)
                    continue
                facts.append(ExtractedFact(
                    source_id=sid, passage=PassageRef(observation_id=obs_id, index=f.passage_index),
                    statement=f.statement.strip(), entities=f.entities,
                ))
                entities.update(dict.fromkeys(f.entities))
    return ExtractionResult(entities=list(entities), facts=facts)
