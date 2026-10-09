"""T5: Extraction and synthesis, driven directly over a seeded SourceStore.

Each test builds the Sources it needs, scripts the three Passage-reading model calls (extract,
draft, entail) and calls the stage. The end-to-end path is covered in test_happy_path."""

import json
import re
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import (
    ExtractedFact,
    ExtractionResult,
    PassageRef,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.pipeline.errors import RunFailed
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.synthesize import bundle_tokens, synthesize
from tests.scripted import ScriptedChat

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
OWNER, RUN = "alice", "run1"
BRIEF = "BYD sales and pricing"


class Light(ScriptedChat):
    """Answers extraction, drafting and entailment calls from per-test handlers, each given the
    numbered passages `[(n, text)]` of the call and returning that call's JSON payload."""

    handlers: dict = {}

    def calls(self, prefix: str) -> list[str]:
        return [str(c[-1].content) for c in self.seen
                if str(next(m.content for m in c if isinstance(m, SystemMessage))).startswith(prefix)]


def light(extract_=None, draft=None, entail=None) -> Light:
    def numbered(user):
        return [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE)]

    def reply(messages):
        system, user = str(messages[0].content), str(messages[-1].content)
        if system.startswith("You extract facts"):
            handler, key = extract_, "facts"
        elif system.startswith("You draft evidence"):
            handler, key = draft, "items"
        elif system.startswith("You judge whether"):
            handler, key = entail, "verdicts"
        else:
            return AIMessage(content="A summary.")
        items = handler(numbered(user), user) if handler else []
        return AIMessage(content=json.dumps({key: items}))

    return Light(reply=reply)


def sees_text(substring):
    """An entailment handler: SUPPORTED where the passage contains `substring`."""
    return lambda ps, user: [
        {"passage": n, "verdict": "SUPPORTED" if substring in t else "INSUFFICIENT_EVIDENCE",
         "asserts_present_state": False} for n, t in ps]


def all_supported(ps, user):
    return [{"passage": n, "verdict": "SUPPORTED"} for n, _ in ps]


def draft_all(**fields):
    """A draft handler: one item per passage, quoting it, with the given structured fields."""
    return lambda ps, user: [
        {"statement": t, "passages": [n], **fields} for n, t in ps]


@pytest.fixture
def world(make_deps):
    class World:
        def __init__(self):
            self.deps = make_deps()
            self.sources: dict[str, str] = {}

        def page(self, url, body, *, cred=0.8, published=None, fetched=NOW, title=None):
            obs = self.deps.store.ingest_web(
                owner_id=OWNER, run_id=RUN, url=url, title=title or url, body=body,
                fetched_at=fetched, credibility_score=cred, published_at=published)
            self.sources[url] = obs.source_id
            return obs.source_id

        def plan(self, *subtasks):
            tasks = [SubTask(id=sid, aspect_id=aid, query=f"q {sid}", source_type="web",
                             time_horizon_months=12, priority=prio)
                     for sid, aid, prio in subtasks]
            aspects = [Aspect(id=a, name=f"Aspect {a}") for a in dict.fromkeys(t.aspect_id for t in tasks)]
            return SubTaskPlan(aspects=aspects, sub_tasks=tasks)

        def research(self, **found):  # subtask id -> source urls
            return ResearchResults(results=[
                SubTaskResult(subtask_id=sid, status="succeeded", source_refs=[
                    SourceRef(source_id=self.sources[u], title=u, summary="s") for u in urls])
                for sid, urls in found.items()])

        def events(self, type_=None):
            evs = self.deps.repos.events.read_after(OWNER, RUN)
            return [e.payload for e in evs if type_ is None or e.type == type_]

        def synth(self, plan, research, extraction=None):
            return synthesize(self.deps, OWNER, RUN, BRIEF, plan, research,
                              extraction or ExtractionResult())

        def items(self, bundle):
            return [i for s in bundle.sections for i in s.items]

    return World()


# =============================== Extraction =================================================

def structured(entity="BYD", predicate="units_sold", scope="global", period="2025"):
    def handler(ps, user):
        return [{"passage_index": n, "statement": t, "entity": entity, "predicate": predicate,
                 "value": re.search(r"[\d.]+ million", t).group(0), "scope": scope,
                 "period": period, "entities": [entity]} for n, t in ps]
    return handler


def test_extraction_reads_one_source_at_a_time_and_stores_facts_as_it_goes(world):
    a = world.page("https://a.test/x", "BYD sold 4.27 million cars.\n\nBYD sold 3.02 million cars.", title="Page A")
    b = world.page("https://b.test/x", "Tesla sold 1.80 million cars.", title="Page B")
    model = light(extract_=lambda ps, user: [
        {"passage_index": n, "statement": t, "entity": "x"} for n, t in ps])
    world.deps.light_model = model
    result = extract(world.deps, OWNER, RUN, [a, b])
    calls = model.calls("You extract facts")
    assert len(calls) == 2
    assert ["Page A" in c and "Page B" not in c for c in calls] == [True, False]
    assert ["Page B" in c and "Page A" not in c for c in calls] == [False, True]
    stored = world.deps.repos.entities.list_for_run(OWNER, RUN)
    assert [f.id for f in stored] == [f.id for f in result.facts]
    assert {f.source_id for f in stored} == {a, b}
    assert all(f.passage.observation_id and f.passage.index >= 0 for f in stored)


def test_extraction_batches_split_at_the_cap_never_truncating(world):
    body = "\n\n".join(f"Paragraph {i}: " + "word " * 40 for i in range(6))  # ~50 tokens each
    sid = world.page("https://a.test/x", body)
    world.deps.passage_token_cap = 120
    model = light(extract_=lambda ps, user: [])
    world.deps.light_model = model
    extract(world.deps, OWNER, RUN, [sid])
    calls = model.calls("You extract facts")
    assert len(calls) == 3
    assert [i for c in calls for i in re.findall(r"^\[(\d+)\]", c, re.MULTILINE)] == [
        str(i) for i in range(6)]  # every Passage, once, in order
    for c in calls:
        assert estimate_tokens(c.split("\n\n", 1)[1]) <= 120 + 10


def test_conflicts_are_found_over_structured_facts_with_no_extra_model_call(world):
    a = world.page("https://a.test/x", "BYD sold 4.27 million cars in 2025.")
    b = world.page("https://b.test/x", "BYD sold 4.01 million cars in 2025.")
    model = light(extract_=structured())
    world.deps.light_model = model
    result = extract(world.deps, OWNER, RUN, [a, b])
    assert len(model.seen) == 2  # one call per Source, none for conflict detection
    (conflict,) = result.conflicts
    assert (conflict.entity, conflict.predicate, conflict.scope) == ("BYD", "units_sold", "global")
    assert set(conflict.fact_ids) == {f.id for f in result.facts}
    assert all(f.conflict for f in result.facts)
    assert all(f.conflict for f in world.deps.repos.entities.list_for_run(OWNER, RUN))
    assert world.events("extraction_conflict")


def test_unknown_period_or_different_scope_is_not_a_conflict(world):
    a = world.page("https://a.test/x", "BYD sold 4.27 million cars.")
    b = world.page("https://b.test/x", "BYD sold 4.01 million cars.")
    outcomes = iter([structured(period="2025"), structured(period="UNKNOWN")])
    world.deps.light_model = light(extract_=lambda ps, user: next(outcomes)(ps, user))
    assert extract(world.deps, OWNER, RUN, [a, b]).conflicts == []
    scopes = iter([structured(scope="global"), structured(scope="china")])
    world.deps.light_model = light(extract_=lambda ps, user: next(scopes)(ps, user))
    assert extract(world.deps, OWNER, RUN, [a, b]).conflicts == []


def test_an_unreadable_period_from_the_model_becomes_unknown(world):
    sid = world.page("https://a.test/x", "BYD sold 4.27 million cars.")
    world.deps.light_model = light(extract_=structured(period="last spring"))
    (fact,) = extract(world.deps, OWNER, RUN, [sid]).facts
    assert fact.period == "UNKNOWN"


# =============================== Synthesis: validation ======================================

def one_source(world, body="BYD sold 4.27 million vehicles in 2025.", **kw):
    world.page("https://a.test/x", body, **kw)
    return world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"])


def test_a_number_not_in_the_passage_is_dropped_and_logged(world):
    plan, research = one_source(world)
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 9.99 million vehicles in 2025.", "passages": [1]}],
        entail=all_supported)
    assert world.items(world.synth(plan, research)) == []
    (dropped,) = world.events("evidence_dropped")
    assert "9.99" in str(dropped["rejected"])


def test_a_named_entity_not_in_the_passage_is_dropped(world):
    plan, research = one_source(world)
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "Tesla sold 4.27 million vehicles in 2025.",
                              "passages": [1], "entities": ["Tesla"]}],
        entail=all_supported)
    assert world.items(world.synth(plan, research)) == []
    assert "Tesla" in str(world.events("evidence_dropped")[0]["rejected"])


def test_a_draft_citing_a_passage_it_was_not_shown_is_dropped(world):
    plan, research = one_source(world)
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.", "passages": [99]}],
        entail=all_supported)
    assert world.items(world.synth(plan, research)) == []
    assert world.events("evidence_dropped")[-1]["reason"] == "draft cites no Passage"


def test_an_item_with_no_supported_passage_is_dropped_and_logged(world):
    plan, research = one_source(world)
    world.deps.light_model = light(draft=draft_all(), entail=lambda ps, u: [
        {"passage": n, "verdict": "INSUFFICIENT_EVIDENCE"} for n, _ in ps])
    assert world.items(world.synth(plan, research)) == []
    (dropped,) = world.events("evidence_dropped")
    assert dropped["reason"] == "no supported Passage" and dropped["evidence_id"].startswith("ev_")


def test_entailment_is_one_call_per_item_and_a_bad_reply_supports_nothing(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.\n\nBYD sold 3.02 million vehicles in 2024.")
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"])
    model = light(draft=draft_all(), entail=all_supported)
    world.deps.light_model = model
    assert len(world.items(world.synth(plan, research))) == 2
    assert len(model.calls("You judge whether")) == 2
    bad = light(draft=draft_all(), entail=lambda ps, u: "not a list")
    world.deps.light_model = bad
    assert world.items(world.synth(plan, research)) == []


def test_candidates_over_the_cap_are_chunked_and_each_chunk_is_a_call(world):
    """One item with many candidate Passages: the entailment call is split at the cap."""
    body = "\n\n".join("BYD sold 4.27 million vehicles in 2025. " + "filler " * 30 for _ in range(4))
    world.page("https://a.test/x", body)
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"])
    world.deps.passage_token_cap = 120
    model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                              "passages": [n for n, _ in ps]}],
        entail=all_supported)
    world.deps.light_model = model
    (item,) = world.items(world.synth(plan, research))
    assert len(item.supporting) == 4
    for call in model.calls("You judge whether") + model.calls("You draft evidence"):
        assert estimate_tokens(call.split("\n\n", 1)[1]) <= 120 + 15


# =============================== Synthesis: Confidence ======================================

def two_sources(world, text_b=None, url_b="https://b.example/y", **kw):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    world.page(url_b, text_b or "BYD sold 4.27 million vehicles in 2025.", cred=0.9, **kw)
    return (world.plan(("s1", "a1", 1)),
            world.research(s1=["https://a.test/x", url_b]))


def only_item(world, plan, research, **kw):
    world.deps.light_model = light(draft=kw.get("draft", draft_all()),
                                   entail=kw.get("entail", all_supported))
    (item,) = world.items(world.synth(plan, research))
    return item


def test_two_independent_sources_give_high_confidence(world):
    plan, research = two_sources(world)
    item = only_item(world, plan, research)
    assert item.confidence == "HIGH" and len(item.supporting) == 2


def test_independence_counts_distinct_sources_never_passages(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.\n\n"
               "BYD sold 4.27 million vehicles in 2025, said a spokesperson.", cred=0.9)
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"])
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                              "passages": [n for n, _ in ps]}], entail=all_supported)
    (item,) = world.items(world.synth(plan, research))
    assert len(item.supporting) == 2 and item.confidence == "MEDIUM"


def test_same_domain_is_not_independent(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    world.page("https://a.test/y", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    item = only_item(world, plan, world.research(s1=["https://a.test/x", "https://a.test/y"]))
    assert item.confidence == "MEDIUM"


def test_a_source_that_cites_the_other_is_not_independent(world):
    plan, research = two_sources(
        world, text_b="BYD sold 4.27 million vehicles in 2025.\n\nSource: https://a.test/x")
    claims_only = lambda ps, u: [  # noqa: E731
        {"statement": t, "passages": [n]} for n, t in ps if t.startswith("BYD")]
    assert only_item(world, plan, research, draft=claims_only).confidence == "MEDIUM"


def test_only_supported_passages_count_toward_confidence(world):
    plan, research = two_sources(world)
    item = only_item(world, plan, research, entail=lambda ps, u: [
        {"passage": n, "verdict": "SUPPORTED" if i == 0 else "INSUFFICIENT_EVIDENCE"}
        for i, (n, _) in enumerate(ps)])
    assert item.confidence == "MEDIUM" and len(item.supporting) == 1


def test_a_single_low_credibility_source_is_low(world):
    plan, research = one_source(world, cred=0.2)
    assert only_item(world, plan, research).confidence == "LOW"


def test_primary_passage_is_the_most_credible_then_most_relevant_then_lowest_id(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.5)
    world.page("https://b.example/y", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.test/x", "https://b.example/y"])
    item = only_item(world, plan, research)
    primary_source = world.deps.store.read_source(OWNER, world.sources["https://b.example/y"])
    assert item.primary.observation_id == primary_source.observation.id
    assert item.primary in item.supporting

    # Equal credibility and relevance: the lowest Passage id wins, whatever order they came in.
    world2_a = world.page("https://c.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://b.example/y", "https://c.test/x"])
    item = only_item(world, plan, research)
    lowest = min(item.supporting, key=lambda r: (r.observation_id, r.index))
    assert item.primary == lowest and world2_a


# =============================== Synthesis: contradictions ==================================

def contradiction_world(world, scope_b="global", period_b="2025"):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    world.page("https://b.example/y", "BYD sold 4.01 million vehicles in 2025.", cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.test/x", "https://b.example/y"])
    sa = world.deps.store.read_source(OWNER, world.sources["https://a.test/x"])
    sb = world.deps.store.read_source(OWNER, world.sources["https://b.example/y"])

    def fact(fid, src, scope, period):
        return ExtractedFact(
            id=fid, source_id=src.source.id,
            passage=PassageRef(observation_id=src.observation.id, index=0),
            statement=src.passages[0].text, entity="BYD", predicate="units_sold", scope=scope,
            value="x", period=period)

    extraction = ExtractionResult(facts=[fact("fa", sa, "global", "2025"),
                                         fact("fb", sb, scope_b, period_b)])
    verdicts = lambda ps, u: [  # noqa: E731
        {"passage": n, "verdict": "SUPPORTED" if "4.27" in t else "CONTRADICTED"} for n, t in ps]
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                              "passages": [n for n, _ in ps], "entity": "BYD",
                              "predicate": "units_sold", "value": "4.27 million",
                              "scope": "global", "as_of_period": "2025"}],
        entail=verdicts)
    return plan, research, extraction


def test_a_genuine_contradiction_marks_the_item_conflicting_and_low(world):
    plan, research, extraction = contradiction_world(world)
    (item,) = world.items(world.synth(plan, research, extraction))
    assert item.conflicting and item.confidence == "LOW"
    assert len(item.supporting) == 1 and len(item.contradicting) == 1
    assert set(item.conflict_fact_ids) == {"fa", "fb"}  # links the Extraction facts
    assert item.supporting[0] != item.contradicting[0]


@pytest.mark.parametrize("scope,period", [("china", "2025"), ("global", "2023")])
def test_a_different_scope_or_period_is_a_distinct_fact_not_a_contradiction(world, scope, period):
    plan, research, extraction = contradiction_world(world, scope_b=scope, period_b=period)
    (item,) = world.items(world.synth(plan, research, extraction))
    assert not item.conflicting and item.contradicting == []
    assert item.confidence in ("MEDIUM", "LOW") and world.events("contradiction_ignored")


# =============================== Synthesis: As-of period ====================================

def asof_item(world, *, stated="UNKNOWN", present=False, published=None):
    plan, research = one_source(world, published=published)
    return only_item(
        world, plan, research,
        draft=draft_all(as_of_period=stated),
        entail=lambda ps, u: [{"passage": n, "verdict": "SUPPORTED",
                               "asserts_present_state": present} for n, _ in ps])


def test_a_stated_period_is_used_as_is(world):
    item = asof_item(world, stated="2025-Q3", present=True)
    assert (item.as_of_period, item.derived_from, item.temporally_ambiguous) == (
        "2025-Q3", None, False)


def test_a_present_state_from_an_undated_source_derives_the_observation_date_and_logs_it(world):
    item = asof_item(world, present=True)
    assert (item.as_of_period, item.derived_from) == ("2026-10-09", "observation_date")
    (logged,) = world.events("as_of_derived")
    assert logged["derived_from"] == "observation_date" and logged["as_of_period"] == "2026-10-09"


@pytest.mark.parametrize("present,published", [
    (False, None),                                  # history, prediction or announcement
    (True, datetime(2025, 12, 1, tzinfo=UTC)),      # the Source has a publication date
])
def test_otherwise_the_period_is_unknown_and_flagged_ambiguous(world, present, published):
    item = asof_item(world, present=present, published=published)
    assert item.as_of_period == "UNKNOWN" and item.temporally_ambiguous
    assert item.derived_from is None and world.events("as_of_derived") == []


# =============================== Synthesis: ranking and budget ==============================

def ranked_world(world, bodies):
    """One Sub-task per aspect-less page; draft quotes each Passage."""
    for n, (body, cred, period) in enumerate(bodies):
        world.page(f"https://s{n}.test/x", body, cred=cred)
    plan = world.plan(*[(f"s{n}", "a1", n + 1) for n in range(len(bodies))])
    research = world.research(**{f"s{n}": [f"https://s{n}.test/x"] for n in range(len(bodies))})
    periods = {body: period for body, _, period in bodies}
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": t, "passages": [n], "as_of_period": periods[t]}
                             for n, t in ps],
        entail=all_supported)
    return plan, research


BODIES = [
    ("BYD sold 4.27 million vehicles in 2025.", 0.9, "2025-06"),
    ("Tesla sold 1.80 million vehicles in 2019.", 0.9, "2019"),
    ("Rivian sold 0.05 million vehicles in 2025.", 0.3, "2025-06"),
    ("Honda sold 1.10 million vehicles in 2025.", 0.9, "2025-08"),
]


def test_ranking_is_deterministic_and_follows_recency_then_credibility(world):
    plan, research = ranked_world(world, BODIES)
    first = [i.statement for i in world.items(world.synth(plan, research))]
    second = [i.statement for i in world.items(world.synth(plan, research))]
    assert first == second
    # fresh+high credibility first (relevance/priority/id break ties), then fresh+low
    # credibility, then the older item last
    assert first[-1].startswith("Tesla") and first[-2].startswith("Rivian")
    assert {s.split()[0] for s in first[:2]} == {"BYD", "Honda"}


def test_bucket_thresholds_are_settings_that_change_the_ranking(world):
    from insightforge_agent.domain.evidence import Thresholds

    plan, research = ranked_world(world, BODIES)
    default = [i.statement for i in world.items(world.synth(plan, research))]
    world.deps.thresholds = Thresholds(recency_fresh_days=10_000, recency_recent_days=20_000,
                                       credibility_high=0.95, credibility_medium=0.95)
    loosened = [i.statement for i in world.items(world.synth(plan, research))]
    assert sorted(loosened) == sorted(default) and loosened != default


def test_bundle_respects_the_budget_and_every_cut_is_logged(world):
    plan, research = ranked_world(world, BODIES)
    full = world.synth(plan, research)
    world.events()  # keep the log of the first pass out of the way
    budget = bundle_tokens(full) - 12
    world.deps.evidence_budget_tokens = budget
    before = len(world.events("evidence_cut"))
    bundle = world.synth(plan, research)
    assert bundle_tokens(bundle) <= budget < bundle_tokens(full)
    cuts = world.events("evidence_cut")[before:]
    kept = {i.id for i in world.items(bundle)}
    all_ids = {i.id for i in world.items(full)}
    assert {c["evidence_id"] for c in cuts} == all_ids - kept
    assert all(c["reason"] == "over evidence budget" and c["confidence"] in ("HIGH", "MEDIUM", "LOW")
               for c in cuts)
    for item in world.items(bundle):  # compressed by dropping whole items, never truncated
        assert item.statement in [b[0] for b in BODIES]


def test_the_lowest_ranked_item_is_the_one_cut(world):
    plan, research = ranked_world(world, BODIES)
    full = world.items(world.synth(plan, research))
    world.deps.evidence_budget_tokens = bundle_tokens(world.synth(plan, research)) - 1
    kept = world.items(world.synth(plan, research))
    assert [i.id for i in kept] == [i.id for i in full[:-1]]


# =============================== Payload capture ============================================

def test_synthesis_payloads_respect_the_per_call_cap_separately_from_the_bundle_budget(world):
    body = "\n\n".join(f"BYD sold {n}.5 million vehicles in 20{n}. " + "filler " * 25
                       for n in range(10, 22))
    world.page("https://a.test/x", body)
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"])
    cap, budget = 150, 150
    world.deps.passage_token_cap = cap
    world.deps.evidence_budget_tokens = budget
    model = light(draft=lambda ps, u: [
        {"statement": t.split(". filler")[0] + ".", "passages": [n]} for n, t in ps],
        entail=all_supported)
    world.deps.light_model = model
    bundle = world.synth(plan, research)
    drafts, entails = model.calls("You draft evidence"), model.calls("You judge whether")
    assert len(drafts) > 3 and entails  # the small cap forced splitting
    for call in drafts + entails:
        passage_text = call.split("\n\n", 1)[1]
        assert estimate_tokens(passage_text) <= cap + 15  # per-call cap, passages only
    assert 0 < bundle_tokens(bundle) <= budget  # bundle budget: a separate limit


def test_a_run_over_its_token_cap_fails_with_the_reason(world):
    plan, research = one_source(world)
    world.deps.light_model = light(draft=draft_all(), entail=all_supported)
    world.deps.run_token_cap = 5
    with pytest.raises(RunFailed, match="token cap"):
        world.synth(plan, research)


def test_a_derived_as_of_period_is_used_for_the_contradiction_check(world):
    plan, research, extraction = contradiction_world(world)
    # The item states no period; both Sources are undated present-state assertions, so the
    # period is derived from the Observation date, and the contradicting fact (dated 2026)
    # overlaps it.
    extraction.facts[1].period = "2026"
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                              "passages": [n for n, _ in ps], "entity": "BYD",
                              "predicate": "units_sold", "value": "4.27 million",
                              "scope": "global"}],
        entail=lambda ps, u: [
            {"passage": n, "verdict": "SUPPORTED" if "4.27" in t else "CONTRADICTED",
             "asserts_present_state": True} for n, t in ps])
    (item,) = world.items(world.synth(plan, research, extraction))
    assert item.derived_from == "observation_date" and item.conflicting
