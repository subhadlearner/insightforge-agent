"""T5 review round: cross-batch contradictions, shared Sources, the hard Passage cap, and
model-supplied structure that the Passage does not state."""

import re

import pytest

from insightforge_agent.domain.contracts import ExtractedFact, ExtractionResult, PassageRef
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.pipeline.extract import extract
from tests.pipeline.test_extraction_synthesis import (  # noqa: F401 - `world` is a fixture
    OWNER,
    RUN,
    all_supported,
    asof_item,
    contradiction_world,
    draft_all,
    light,
    world,
)


# =============================== Cross-batch contradictions =================================

def split_batch_world(world, *, value_b="4.01 million", scope_b="global", period_b="2025",
                      text_b=None):
    """Two Sources whose Passages land in different drafting batches, with Extraction facts
    that say they disagree. Neither draft cites the other's Passage."""
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    world.page("https://b.example/y", text_b or "BYD sold 4.01 million vehicles in 2025.", cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.test/x", "https://b.example/y"])
    world.deps.passage_token_cap = 14  # one page per call

    def fact(url, fid, value, scope, period):
        src = world.deps.store.read_source(OWNER, world.sources[url])
        return ExtractedFact(
            id=fid, source_id=src.source.id,
            passage=PassageRef(observation_id=src.observation.id, index=0),
            statement=src.passages[0].text, entity="BYD", predicate="units_sold", scope=scope,
            value=value, period=period)

    extraction = ExtractionResult(facts=[
        fact("https://a.test/x", "f_a", "4.27 million", "global", "2025"),
        fact("https://b.example/y", "f_b", value_b, scope_b, period_b)])

    def draft(ps, user):
        return [{"statement": t, "passages": [n], "entity": "BYD", "predicate": "units_sold",
                 "value": re.search(r"[\d.]+ million", t).group(0),
                 "scope": "china" if "China" in t else "global",
                 "as_of_period": re.search(r"\d{4}", t).group(0)} for n, t in ps]

    def entail(ps, user):
        number = re.search(r"Statement: .*?([\d.]+) million", user).group(1)
        return [{"passage": n, "verdict": "SUPPORTED" if number in t else "CONTRADICTED"}
                for n, t in ps]

    model = light(draft=draft, entail=entail)
    world.deps.light_model = model
    return plan, research, extraction, model


def test_a_contradiction_in_another_drafting_batch_still_marks_both_items_conflicting(world):
    plan, research, extraction, model = split_batch_world(world)
    items = world.items(world.synth(plan, research, extraction))
    assert len(items) == 2
    drafts = model.calls("You draft evidence")
    assert len(drafts) == 2 and all(("4.27" in d) != ("4.01" in d) for d in drafts)  # separate
    for item in items:
        assert item.conflicting and item.confidence == "LOW"
        assert len(item.supporting) == 1 and len(item.contradicting) == 1
        assert item.contradicting[0] != item.supporting[0]
        assert set(item.conflict_fact_ids) == {"f_a", "f_b"}


@pytest.mark.parametrize("kwargs", [
    dict(value_b="4.27 million", text_b="BYD sold 4.27 million vehicles in 2025, reports say."),
    dict(scope_b="china", text_b="BYD sold 4.01 million vehicles in China in 2025."),
    dict(period_b="2023", text_b="BYD sold 4.01 million vehicles in 2023."),
])
def test_equal_values_other_scopes_and_other_periods_are_not_conflicts(world, kwargs):
    plan, research, extraction, model = split_batch_world(world, **kwargs)
    items = world.items(world.synth(plan, research, extraction))
    assert items
    for item in items:
        assert not item.conflicting and item.contradicting == [] and item.conflict_fact_ids == []


def test_a_mistaken_contradicted_verdict_over_an_equal_value_creates_no_conflict(world):
    plan, research, extraction, _ = split_batch_world(
        world, value_b="4.27 million", text_b="BYD sold 4.27 million vehicles in 2025, reports say.")
    # one draft cites both Passages, and the model wrongly calls the second CONTRADICTED
    world.deps.passage_token_cap = 3000
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                              "passages": [n for n, _ in ps], "entity": "BYD",
                              "predicate": "units_sold", "value": "4.27 million", "scope": "global",
                              "as_of_period": "2025"}],
        entail=lambda ps, u: [{"passage": n, "verdict": "SUPPORTED" if i == 0 else "CONTRADICTED"}
                              for i, (n, _) in enumerate(ps)])
    (item,) = world.items(world.synth(plan, research, extraction))
    assert not item.conflicting and item.contradicting == []
    assert world.events("contradiction_ignored")


# =============================== Shared Sources =============================================

def test_a_source_found_by_two_subtasks_is_read_for_each_and_neither_aspect_disappears(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.\n\n"
               "Tesla cut the Model 3 price to 32000 dollars in China.", cred=0.9)
    plan = world.plan(("s1", "a1", 1), ("s2", "a2", 2))
    research = world.research(s1=["https://a.test/x"], s2=["https://a.test/x"])

    def draft(ps, user):
        word = "sold" if "q s1" in user else "price"
        return [{"statement": t, "passages": [n]} for n, t in ps if word in t]

    model = light(draft=draft, entail=all_supported)
    world.deps.light_model = model
    bundle = world.synth(plan, research)
    assert {s.aspect_id for s in bundle.sections} == {"a1", "a2"}
    assert len(model.calls("You draft evidence")) == 2  # once per Sub-task
    assert {i.subtask_id for i in world.items(bundle)} == {"s1", "s2"}


def test_the_same_evidence_from_a_shared_source_is_merged_into_the_most_important_subtask(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.9)
    plan = world.plan(("s1", "a1", 2), ("s2", "a2", 1))
    research = world.research(s1=["https://a.test/x"], s2=["https://a.test/x"])
    world.deps.light_model = light(draft=draft_all(), entail=all_supported)
    (item,) = world.items(world.synth(plan, research))
    assert item.subtask_id == "s2"


# =============================== The hard Passage cap =======================================

def test_an_oversized_passage_is_read_in_bounded_windows_by_extraction_and_synthesis(world):
    body = "BYD sold 4.27 million vehicles in 2025. " + "filler " * 300  # ~540 tokens, one Passage
    sid = world.page("https://a.test/x", body)
    cap = 100
    world.deps.passage_token_cap = cap
    observation = world.deps.store.read_source(OWNER, sid).observation
    stored_before = world.deps.repos.passages.list(OWNER, observation.id)
    assert len(stored_before) == 1

    def content_of(call):
        return "\n\n".join(re.findall(r"^\[\d+\] (?:\(part \d+ of \d+\) )?(.+)$", call, re.MULTILINE))

    statement = "BYD sold 4.27 million vehicles in 2025."
    model = light(
        extract_=lambda ps, u: [{"passage_index": 0, "statement": statement}
                                for _, t in ps if "4.27" in t],
        draft=lambda ps, u: [{"statement": statement, "passages": [n]} for n, t in ps if "4.27" in t],
        entail=lambda ps, u: [{"passage": n, "verdict": "SUPPORTED" if "4.27" in t
                               else "INSUFFICIENT_EVIDENCE"} for n, t in ps])
    world.deps.light_model = model
    extraction = extract(world.deps, OWNER, RUN, [sid])
    (fact,) = extraction.facts
    bundle = world.synth(world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"]),
                         extraction)
    assert len(model.calls("You extract facts")) > 1 and len(model.calls("You judge whether")) > 1
    for call in (model.calls("You extract facts") + model.calls("You draft evidence")
                 + model.calls("You judge whether")):
        assert estimate_tokens(content_of(call)) <= cap
    (item,) = world.items(bundle)
    assert world.deps.repos.passages.list(OWNER, observation.id) == stored_before  # not re-split
    assert item.primary == fact.passage == PassageRef(observation_id=observation.id, index=0)


# =============================== Model-supplied structure ===================================

def test_a_correct_statement_with_a_fabricated_value_does_not_become_a_conflict(world):
    plan, research, extraction = contradiction_world(world)
    world.deps.light_model = light(
        draft=lambda ps, u: [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                              "passages": [n for n, _ in ps], "entity": "BYD",
                              "predicate": "units_sold", "value": "9.99 million",
                              "scope": "global", "as_of_period": "2025"}],
        entail=lambda ps, u: [
            {"passage": n, "verdict": "SUPPORTED" if "4.27" in t else "CONTRADICTED"}
            for n, t in ps])
    (item,) = world.items(world.synth(plan, research, extraction))
    assert item.value == "" and not item.conflicting and item.contradicting == []
    assert world.events("value_rejected")


def test_a_period_the_passage_does_not_state_is_not_accepted(world):
    item = asof_item(world, stated="2019")  # the Passage says 2025
    assert item.as_of_period == "UNKNOWN" and item.temporally_ambiguous
    assert world.events("period_rejected")


def test_a_fabricated_period_does_not_block_the_observation_date_derivation(world):
    item = asof_item(world, stated="2019", present=True)
    assert (item.as_of_period, item.derived_from) == ("2026-10-09", "observation_date")


def test_extraction_clears_a_structured_value_the_passage_does_not_state(world):
    a = world.page("https://a.test/x", "BYD sold 4.27 million cars in 2025.")
    b = world.page("https://b.test/x", "BYD sold 4.01 million cars in 2025.")
    values = iter(["9.99 million", "4.01 million"])  # the first is fabricated
    world.deps.light_model = light(extract_=lambda ps, u: [
        {"passage_index": n, "statement": t, "entity": "BYD", "predicate": "units_sold",
         "value": next(values), "scope": "global", "period": "2025"} for n, t in ps])
    result = extract(world.deps, OWNER, RUN, [a, b])
    assert result.conflicts == [] and not any(f.conflict for f in result.facts)
    assert [f.value for f in result.facts] == ["", "4.01 million"]
    assert world.events("extraction_value_rejected")


def test_extraction_replaces_a_period_the_passage_does_not_state_with_unknown(world):
    sid = world.page("https://a.test/x", "BYD sold 4.27 million cars in 2025.")
    world.deps.light_model = light(extract_=lambda ps, u: [
        {"passage_index": n, "statement": t, "entity": "BYD", "predicate": "units_sold",
         "value": "4.27 million", "period": "2019"} for n, t in ps])
    (fact,) = extract(world.deps, OWNER, RUN, [sid]).facts
    assert fact.period == "UNKNOWN" and fact.value == "4.27 million"
    assert world.events("extraction_period_rejected")
