"""T5 final round: the `confidence_decided` event, and corroboration across number formats."""

import json
import re

import pytest

from insightforge_agent.domain.contracts import ExtractedFact, ExtractionResult, PassageRef
from tests.pipeline.test_extraction_synthesis import (  # noqa: F401 - `world` is a fixture
    OWNER,
    all_supported,
    contradiction_world,
    draft_all,
    light,
    world,
)

STATEMENT = "BYD sold 4.27 million vehicles in 2025."


def decided(world):
    return {e["evidence_id"]: e for e in world.events("confidence_decided")}


def run_one(world, plan_research, **kw):
    plan, research = plan_research
    world.deps.light_model = light(draft=kw.get("draft", draft_all()),
                                   entail=kw.get("entail", all_supported))
    return world.items(world.synth(plan, research, kw.get("extraction")))


def two_domains(world, cred=0.6):
    world.page("https://a.example.com/x", STATEMENT, cred=cred)
    world.page("https://b.other.org/y", STATEMENT + " (b)", cred=cred)
    return world.plan(("s1", "a1", 1)), world.research(
        s1=["https://a.example.com/x", "https://b.other.org/y"])


# =============================== confidence_decided =========================================

def test_two_independent_sources_are_logged_as_independent_sources(world):
    (item,) = run_one(world, two_domains(world, cred=0.3), draft=lambda ps, u: [
        {"statement": STATEMENT, "passages": [n for n, _ in ps]}])
    event = decided(world)[item.id]
    assert item.confidence == "HIGH"
    assert event["confidence"] == "HIGH" and event["decision_rule"] == "independent_sources"
    assert event["supported_passage_count"] == 2 and event["distinct_supporting_source_count"] == 2
    assert event["independent_pair_found"] is True
    assert event["best_source_credibility"] == pytest.approx(0.3)
    assert event["credibility_high_threshold"] == world.deps.thresholds.credibility_high
    assert event["conflicting"] is False and len(event["source_ids"]) == 2


def test_many_passages_of_one_source_are_one_distinct_source(world):
    world.page("https://a.example.com/x", STATEMENT + "\n\n" + STATEMENT + " Again.", cred=0.9)
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.example.com/x"])
    (item,) = run_one(world, (plan, research), draft=lambda ps, u: [
        {"statement": STATEMENT, "passages": [n for n, _ in ps]}])
    event = decided(world)[item.id]
    assert event["supported_passage_count"] == 2 and event["distinct_supporting_source_count"] == 1
    assert event["independent_pair_found"] is False
    assert event["decision_rule"] == "high_credibility_single_source" and item.confidence == "MEDIUM"


def test_a_single_low_credibility_source_is_insufficient_credibility(world):
    world.page("https://a.example.com/x", STATEMENT, cred=0.5)
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.example.com/x"])
    (item,) = run_one(world, (plan, research))
    event = decided(world)[item.id]
    assert (event["confidence"], event["decision_rule"]) == ("LOW", "insufficient_credibility")
    assert event["best_source_credibility"] == pytest.approx(0.5)


def test_same_domain_sources_are_two_sources_but_no_independent_pair(world):
    world.page("https://a.example.com/x", STATEMENT, cred=0.9)
    world.page("https://a.example.com/y", STATEMENT + " (y)", cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.example.com/x", "https://a.example.com/y"])
    (item,) = run_one(world, (plan, research), draft=lambda ps, u: [
        {"statement": STATEMENT, "passages": [n for n, _ in ps]}])
    event = decided(world)[item.id]
    assert event["distinct_supporting_source_count"] == 2
    assert event["independent_pair_found"] is False and item.confidence == "MEDIUM"


def test_a_conflicting_item_is_logged_as_conflicting_low_whatever_the_sources(world):
    plan, research, extraction = contradiction_world(world)
    (item,) = world.items(world.synth(plan, research, extraction))
    event = decided(world)[item.id]
    assert (event["confidence"], event["decision_rule"], event["conflicting"]) == (
        "LOW", "conflicting", True)


def test_the_event_is_logged_for_items_the_budget_later_cuts_and_holds_no_passage_text(world):
    plan, research = two_domains(world)
    world.deps.light_model = light(draft=draft_all(), entail=all_supported)
    full = world.synth(plan, research)
    world.deps.evidence_budget_tokens = 1
    world.synth(plan, research)
    cut = {e["evidence_id"] for e in world.events("evidence_cut")}
    assert cut and cut <= {e["evidence_id"] for e in world.events("confidence_decided")}
    assert len(world.items(full)) >= 1
    for event in world.events("confidence_decided"):
        assert STATEMENT not in json.dumps(event)
        assert set(event) == {
            "evidence_id", "confidence", "decision_rule", "supported_passage_count",
            "distinct_supporting_source_count", "independent_pair_found",
            "best_source_credibility", "credibility_high_threshold", "conflicting", "source_ids"}


def test_items_that_are_dropped_get_no_confidence_event(world):
    world.page("https://a.example.com/x", STATEMENT)
    plan, research = world.plan(("s1", "a1", 1)), world.research(s1=["https://a.example.com/x"])
    run_one(world, (plan, research), entail=lambda ps, u: [
        {"passage": n, "verdict": "INSUFFICIENT_EVIDENCE"} for n, _ in ps])
    assert world.events("confidence_decided") == []


# =============================== numeric corroboration ======================================

def split_world(world, text_b, value_a="4.27 million", value_b="4,270,000", period_b="2025"):
    world.page("https://a.example.com/x", STATEMENT, cred=0.6)
    world.page("https://b.other.org/y", text_b, cred=0.6)
    world.deps.passage_token_cap = 18  # each page is drafted on its own
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.example.com/x", "https://b.other.org/y"])
    spec = {STATEMENT: (value_a, "2025"), text_b: (value_b, period_b)}

    def draft(ps, user):
        return [{"statement": t, "passages": [n], "entity": "BYD", "predicate": "units_sold",
                 "value": spec[t][0], "scope": "global", "as_of_period": spec[t][1]}
                for n, t in ps]

    world.deps.light_model = light(draft=draft, entail=all_supported)
    return plan, research


def test_equivalent_number_formats_in_separate_batches_corroborate_each_other(world):
    text_b = "BYD sold 4,270,000 vehicles in 2025."
    plan, research = split_world(world, text_b)
    (item,) = world.items(world.synth(plan, research))
    assert item.confidence == "HIGH" and len(item.supporting) == 2
    assert item.statement == STATEMENT  # the first statement survives unchanged
    assert item.value == "4.27 million"
    # provenance is intact: each supporting Passage is a real stored Passage holding its own text
    texts = sorted(world.deps.store.get_passages(OWNER, r.observation_id, [r.index])[0].text
                   for r in item.supporting)
    assert texts == sorted([STATEMENT, text_b])
    event = decided(world)[item.id]
    assert event["decision_rule"] == "independent_sources"
    assert event["distinct_supporting_source_count"] == 2


def test_a_passage_is_never_counted_for_a_statement_it_does_not_state(world):
    """B's structured value claims the same key as A's ("4,270,000"), but its Passage says
    billion. The key would merge them; the per-Passage literal check keeps B's Passage out."""
    plan, research = split_world(
        world, "BYD sold 4.27 billion vehicles in 2025.", value_b="4,270,000")
    (item,) = world.items(world.synth(plan, research))
    assert item.statement == STATEMENT and len(item.supporting) == 1
    (ref,) = item.supporting
    assert "4.27 million" in world.deps.store.get_passages(OWNER, ref.observation_id, [ref.index])[0].text
    assert item.confidence == "LOW"


@pytest.mark.parametrize("text_b,value_b,period_b", [
    ("BYD sold 4.27 billion vehicles in 2025.", "4.27 billion", "2025"),
    ("BYD sold 4.27 million dollars in 2025.", "4.27 million dollars", "2025"),
    ("BYD sold about 4.27 million vehicles in 2025.", "about 4.27 million", "2025"),
    ("BYD sold 4.3 million vehicles in 2025.", "4.3 million", "2025"),
    ("BYD sold 4.27 million vehicles in December 2025.", "4.27 million", "2025-12"),
])
def test_non_equivalent_values_and_periods_are_not_merged(world, text_b, value_b, period_b):
    plan, research = split_world(world, text_b, value_a="4.27 million", value_b=value_b,
                                 period_b=period_b)
    items = world.items(world.synth(plan, research))
    assert len(items) == 2
    assert all(len(i.supporting) == 1 and i.confidence == "LOW" for i in items)


def test_equivalent_formats_are_not_challengers_or_conflicts(world):
    text_b = "BYD sold 4,270,000 vehicles in 2025."
    plan, research = split_world(world, text_b)

    def fact(url, fid, value):
        src = world.deps.store.read_source(OWNER, world.sources[url])
        return ExtractedFact(
            id=fid, source_id=src.source.id,
            passage=PassageRef(observation_id=src.observation.id, index=0),
            statement=src.passages[0].text, entity="BYD", predicate="units_sold",
            scope="global", value=value, period="2025")

    extraction = ExtractionResult(facts=[fact("https://a.example.com/x", "f_a", "4.27 million"),
                                         fact("https://b.other.org/y", "f_b", "4,270,000")])
    (item,) = world.items(world.synth(plan, research, extraction))
    assert not item.conflicting and item.contradicting == [] and item.confidence == "HIGH"


def test_a_real_disagreement_in_another_number_format_is_still_a_conflict(world):
    plan, research = split_world(world, "BYD sold 4,010,000 vehicles in 2025.",
                                 value_b="4,010,000")

    def fact(url, fid, value):
        src = world.deps.store.read_source(OWNER, world.sources[url])
        return ExtractedFact(
            id=fid, source_id=src.source.id,
            passage=PassageRef(observation_id=src.observation.id, index=0),
            statement=src.passages[0].text, entity="BYD", predicate="units_sold",
            scope="global", value=value, period="2025")

    extraction = ExtractionResult(facts=[fact("https://a.example.com/x", "f_a", "4.27 million"),
                                         fact("https://b.other.org/y", "f_b", "4,010,000")])

    def entail(ps, user):
        number = re.search(r"Statement: .*?([\d.,]+) (?:million|vehicles)", user).group(1)
        return [{"passage": n, "verdict": "SUPPORTED" if number in t else "CONTRADICTED"}
                for n, t in ps]

    world.deps.light_model = light(
        draft=lambda ps, u: [
            {"statement": t, "passages": [n], "entity": "BYD", "predicate": "units_sold",
             "value": "4.27 million" if "4.27" in t else "4,010,000", "scope": "global",
             "as_of_period": "2025"} for n, t in ps],
        entail=entail)
    items = world.items(world.synth(plan, research, extraction))
    assert len(items) == 2 and all(i.conflicting and i.confidence == "LOW" for i in items)
