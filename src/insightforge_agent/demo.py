"""Offline acceptance demo for Extraction and synthesis (T5).

    uv run insightforge-agent demo

Runs the real `extract` and `synthesize` stages over seeded Sources, in memory. The only thing
that is not production code is the model: `ScriptedModel` answers the three Passage-reading
calls (extract, draft, entail) from small tables below, standing in for an LLM. No network, no
credentials, no API calls. Everything the demo shows (merging, validation, Confidence,
contradictions, ranking, budget) is decided by the production pipeline.

The scripted entailment judge follows a simple rule and uses `unsupported_numbers` for its
number comparison; a real model judges meaning, so this only shows the pipeline's reaction to
each verdict."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    ExtractionResult,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.models import RunEvent
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.quantities import unsupported_numbers
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.synthesize import bundle_tokens, synthesize
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.source_store import SourceStore

OWNER, RUN = "demo", "demo-run"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
BRIEF = "BYD and Tesla electric vehicle sales in 2025."

# --- seeded Sources: (url, title, credibility, body) -----------------------------------------

SOURCES = {
    "news": ("https://news.example.com/byd", "BYD annual sales", 0.6,
             "BYD sold 4.27 million vehicles in 2025."),
    "report": ("https://reports.example.org/byd", "BYD deliveries report", 0.6,
               "BYD delivered 4,270,000 vehicles in 2025.\n\n"
               "Plug-in hybrids made up about half of those sales."),
    "agency": ("https://energy.example.gov/outlook", "Global EV outlook", 0.8,
               "Global EV sales reached 17 million in 2025."),
    "blog_a": ("https://blog-a.example.net/tesla", "Tesla sales (blog A)", 0.6,
               "Tesla sold 1.80 million vehicles in 2025."),
    "blog_b": ("https://blog-b.example.io/tesla", "Tesla sales (blog B)", 0.6,
               "Tesla sold 1.64 million vehicles in 2025."),
}
# Sub-task id -> (Aspect id, query, priority, Sources it found)
SUBTASKS = {
    "s1": ("a1", "BYD 2025 sales", 1, ["news", "report"]),
    "s2": ("a2", "Global EV market 2025", 2, ["agency"]),
    "s3": ("a3", "Tesla 2025 sales", 3, ["blog_a", "blog_b"]),
}
ASPECTS = {"a1": "BYD sales", "a2": "Market size", "a3": "Tesla sales"}

# Structured facts the scripted extractor reports: passage text -> (entity, predicate, value, period)
FACTS = {
    SOURCES["news"][3]: ("BYD", "units_sold", "4.27 million", "2025"),
    "BYD delivered 4,270,000 vehicles in 2025.": ("BYD", "units_sold", "4,270,000", "2025"),
    SOURCES["agency"][3]: ("Global EV", "units_sold", "17 million", "2025"),
    SOURCES["blog_a"][3]: ("Tesla", "units_sold", "1.80 million", "2025"),
    SOURCES["blog_b"][3]: ("Tesla", "units_sold", "1.64 million", "2025"),
}
# Candidates the scripted drafter adds on top of "one item per known Passage": a fabricated
# figure and a claim no Passage states. The pipeline must reject both.
FABRICATED = "BYD sold 4.9 million vehicles in 2025."
UNSUPPORTED = "BYD is the safest electric vehicle brand."


class ScriptedModel(BaseChatModel):
    """A chat model that answers every call with `reply(system, user)` (JSON text)."""

    reply: Callable[[str, str], str]
    calls: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-demo"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        system, user = str(messages[0].content), str(messages[-1].content)
        self.calls.append(system.split("\n", 1)[0])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=self.reply(system, user)))])


def _numbered(user: str) -> list[tuple[int, str]]:
    return [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE)]


def _extract_reply(user: str) -> dict:
    return {"facts": [
        {"passage_index": n, "statement": t, "entity": FACTS[t][0], "predicate": FACTS[t][1],
         "value": FACTS[t][2], "period": FACTS[t][3]} for n, t in _numbered(user) if t in FACTS]}


def _draft_reply(user: str) -> dict:
    items = []
    for n, text in _numbered(user):
        entity, predicate, value, period = FACTS.get(text, ("", "", "", "UNKNOWN"))
        items.append({"statement": text, "passages": [n], "entity": entity, "predicate": predicate,
                      "value": value, "as_of_period": period})
        if "BYD sold 4.27 million" in text:  # the planted bad candidates cite the BYD Passage
            items.append({"statement": FABRICATED, "passages": [n]})
            items.append({"statement": UNSUPPORTED, "passages": [n]})
    return {"items": items}


def _judge(statement: str, passage: str) -> str:
    """The scripted entailment rule. A statement about another subject is INSUFFICIENT; a
    statement with figures is SUPPORTED if the Passage states them and CONTRADICTED if not; a
    statement with no figures is SUPPORTED only if the Passage says it."""
    if statement.split()[0].casefold() not in passage.casefold():
        return "INSUFFICIENT_EVIDENCE"
    if re.search(r"\d", statement):
        return "CONTRADICTED" if unsupported_numbers(statement, passage) else "SUPPORTED"
    return "SUPPORTED" if statement.casefold() in passage.casefold() else "INSUFFICIENT_EVIDENCE"


def _entail_reply(user: str) -> dict:
    statement = re.search(r"^Statement: (.+)$", user, re.MULTILINE).group(1)
    return {"verdicts": [{"passage": n, "verdict": _judge(statement, t)}
                         for n, t in _numbered(user)]}


def build_model() -> ScriptedModel:
    def reply(system: str, user: str) -> str:
        if system.startswith("You extract facts"):
            return json.dumps(_extract_reply(user))
        if system.startswith("You draft evidence"):
            return json.dumps(_draft_reply(user))
        return json.dumps(_entail_reply(user))
    return ScriptedModel(reply=reply)


# --- the run ----------------------------------------------------------------------------------

@dataclass
class DemoRun:
    deps: Deps
    extraction: ExtractionResult
    bundle: EvidenceBundle
    events: list[RunEvent]
    locators: dict[str, str]  # source id -> url


def run_demo() -> DemoRun:
    repos = MemoryRepos()
    store = SourceStore(repos.sources, repos.observations, repos.passages)
    model = build_model()
    deps = Deps(repos=repos, store=store, planner_model=model, writer_model=model,
                light_model=model, researcher_model=model, search=None, fetcher=None,
                checkpointer=None, now=lambda: NOW)  # extract and synthesize use only the store
    ids = {}
    for key, (url, title, credibility, body) in SOURCES.items():
        ids[key] = store.ingest_web(
            owner_id=OWNER, run_id=RUN, url=url, title=title, body=body, fetched_at=NOW,
            credibility_score=credibility).source_id
    plan = SubTaskPlan(
        aspects=[Aspect(id=i, name=n) for i, n in ASPECTS.items()],
        sub_tasks=[SubTask(id=sid, aspect_id=a, query=q, source_type="web",
                           time_horizon_months=12, priority=p)
                   for sid, (a, q, p, _) in SUBTASKS.items()])
    research = ResearchResults(results=[
        SubTaskResult(subtask_id=sid, status="succeeded", source_refs=[
            SourceRef(source_id=ids[k], title=SOURCES[k][1], summary="") for k in keys])
        for sid, (_, _, _, keys) in SUBTASKS.items()])
    extraction = extract(deps, OWNER, RUN, list(ids.values()))
    bundle = synthesize(deps, OWNER, RUN, BRIEF, plan, research, extraction)
    return DemoRun(deps, extraction, bundle, repos.events.read_after(OWNER, RUN),
                   {v: SOURCES[k][0] for k, v in ids.items()})


# --- the report -------------------------------------------------------------------------------

def _host(url: str) -> str:
    return url.split("/")[2]


def checks(run: DemoRun) -> list[tuple[str, bool]]:
    items = [i for s in run.bundle.sections for i in s.items]
    decided = {e.payload["evidence_id"]: e.payload for e in run.events if e.type == "confidence_decided"}
    byd = [i for i in items if i.entity == "BYD"]
    tesla = [i for i in items if i.entity == "Tesla"]
    agency = [i for i in items if i.entity == "Global EV"]
    dropped = {e.payload["statement"]: e.payload["reason"]
               for e in run.events if e.type == "evidence_dropped"}
    return [
        ('"4.27 million" and "4,270,000" corroborate: one HIGH item with 2 independent Sources',
         len(byd) == 1 and byd[0].confidence == "HIGH" and len(byd[0].supporting) == 2
         and decided[byd[0].id]["decision_rule"] == "independent_sources"),
        ("equivalent number formats are not reported as a conflict",
         not any(f.conflict for f in run.extraction.facts if f.entity == "BYD")),
        ("contradicting Tesla figures: both items conflicting, LOW, with Extraction facts linked",
         len(tesla) == 2 and all(i.conflicting and i.confidence == "LOW" and i.conflict_fact_ids
                                 and i.contradicting for i in tesla)
         and len(run.extraction.conflicts) == 1),
        ("a single trusted-domain Source is MEDIUM (high_credibility_single_source)",
         len(agency) == 1 and agency[0].confidence == "MEDIUM"
         and decided[agency[0].id]["decision_rule"] == "high_credibility_single_source"),
        ("a fabricated figure is rejected by the literal-value check",
         FABRICATED in dropped and "value not in Passage" in str(
             next(e.payload["rejected"] for e in run.events
                  if e.type == "evidence_dropped" and e.payload["statement"] == FABRICATED))),
        ("a claim no Passage supports is dropped (no supported Passage)",
         dropped.get(UNSUPPORTED) == "no supported Passage"),
        ("the bundle fits the token budget",
         bundle_tokens(run.bundle) <= run.deps.evidence_budget_tokens),
    ]


def render(run: DemoRun) -> str:
    store, out = run.deps.store, []
    say = out.append

    def where(ref) -> str:
        p = store.get_passages(OWNER, ref.observation_id, [ref.index])[0]
        source = store.read_source(
            OWNER, run.deps.repos.observations.get(OWNER, ref.observation_id).source_id)
        return f'{_host(source.source.locator)} (credibility {source.source.credibility_score:.2f}): "{p.text}"'

    say("INSIGHTFORGE T5 OFFLINE DEMO: scripted models, seeded Sources, no network\n")
    say("1. EXTRACTION")
    say(f"   {len(run.extraction.facts)} structured facts, {len(run.extraction.conflicts)} conflict(s)")
    for c in run.extraction.conflicts:
        say(f"   conflict: {c.entity} / {c.predicate} / {c.scope or 'any scope'}: facts {', '.join(c.fact_ids)}")

    say("\n2. EVIDENCE BUNDLE (ranked, budgeted)")
    decided = {e.payload["evidence_id"]: e.payload for e in run.events if e.type == "confidence_decided"}
    for section in run.bundle.sections:
        say(f"   {section.heading}  [section {section.confidence}]")
        for item in section.items:
            d = decided[item.id]
            say(f'     {item.id} {item.confidence}  rule={d["decision_rule"]}  '
                f'sources={d["distinct_supporting_source_count"]}  as-of={item.as_of_period}')
            say(f'       "{item.statement}"')
            for ref in item.supporting:
                say(f"       supported by {where(ref)}")
            for ref in item.contradicting:
                say(f"       contradicted by {where(ref)}")
    say(f"   bundle size {bundle_tokens(run.bundle)} tokens of {run.deps.evidence_budget_tokens}")

    say("\n3. REJECTED")
    for e in run.events:
        if e.type == "evidence_dropped":
            say(f'   "{e.payload["statement"]}"\n       -> {e.payload["reason"]}'
                + (f" {e.payload['rejected']}" if e.payload.get("rejected") else ""))

    say("\n4. WHAT THIS SHOWS")
    results = checks(run)
    for text, ok in results:
        say(f"   [{'PASS' if ok else 'FAIL'}] {text}")
    return "\n".join(out)


def main() -> int:
    run = run_demo()
    print(render(run))
    return 0 if all(ok for _, ok in checks(run)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
