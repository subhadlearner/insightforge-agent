from datetime import date

from insightforge_agent.domain.contracts import EvidenceItem, ExtractedFact, PassageRef
from insightforge_agent.domain.evidence import (
    Thresholds,
    compress_to_budget,
    confidence_of,
    credibility_bucket,
    rank_key,
    recency_bucket,
    section_label,
)
from insightforge_agent.domain.extraction import detect_conflicts

T = Thresholds()
REF = PassageRef(observation_id="o", index=0)


def fact(fid, value, period="2025", entity="BYD", predicate="sales", scope="global"):
    return ExtractedFact(id=fid, source_id="s", passage=REF, statement=f"{entity} {value}",
                         entity=entity, predicate=predicate, scope=scope, value=value,
                         period=period)


def item(iid, aspect="a1", relevance=0.5, confidence="MEDIUM"):
    return EvidenceItem(id=iid, subtask_id="s1", aspect_id=aspect, statement=iid * 10,
                        confidence=confidence, supporting=[REF], primary=REF, relevance=relevance)


# --- conflicts ------------------------------------------------------------------------------

def test_different_values_for_overlapping_periods_conflict():
    facts, conflicts = detect_conflicts(
        [fact("f1", "4.27 million"), fact("f2", "4.1 million", "2025-Q3")])
    assert [c.fact_ids for c in conflicts] == [["f1", "f2"]]
    assert all(f.conflict for f in facts)


def test_same_value_spacing_case_and_separators_do_not_conflict():
    facts, conflicts = detect_conflicts([fact("f1", "4,270,000"), fact("f2", " 4270000 ")])
    assert conflicts == [] and not any(f.conflict for f in facts)


def test_non_overlapping_periods_different_scopes_and_unknown_periods_are_distinct_facts():
    for other in (fact("f2", "9", period="2024"), fact("f2", "9", scope="china"),
                  fact("f2", "9", period="UNKNOWN"), fact("f2", "9", entity="Tesla")):
        facts, conflicts = detect_conflicts([fact("f1", "4"), other])
        assert conflicts == [] and not any(f.conflict for f in facts)


def test_facts_without_a_structure_are_never_compared():
    assert detect_conflicts([fact("f1", "4"), fact("f2", "")])[1] == []


def test_only_the_conflicting_facts_of_a_group_are_marked():
    facts, conflicts = detect_conflicts(
        [fact("f1", "4", "2025"), fact("f2", "5", "2025"), fact("f3", "6", "2020")])
    assert conflicts[0].fact_ids == ["f1", "f2"]
    assert [f.conflict for f in facts] == [True, True, False]


# --- buckets, confidence, ranking -----------------------------------------------------------

def test_buckets_follow_the_thresholds():
    assert [credibility_bucket(s, T) for s in (0.9, 0.65, 0.5, 0.1)] == [2, 2, 1, 0]
    today = date(2026, 1, 1)
    assert [recency_bucket(p, today, T) for p in ("2025-12", "2024", "2018", "UNKNOWN")] == [
        2, 1, 0, 0]
    assert recency_bucket("2018", today, Thresholds(recency_fresh_days=10_000)) == 2


def test_confidence_rules():
    def conf(**kw):
        return confidence_of(high=0.65, **kw)

    assert conf(has_independent_pair=True, best_credibility=0.1, conflicting=False) == "HIGH"
    assert conf(has_independent_pair=False, best_credibility=0.7, conflicting=False) == "MEDIUM"
    assert conf(has_independent_pair=False, best_credibility=0.2, conflicting=False) == "LOW"
    assert conf(has_independent_pair=True, best_credibility=0.9, conflicting=True) == "LOW"
    assert section_label(["HIGH", "LOW", "MEDIUM"]) == "LOW"


def test_ranking_follows_the_tuple_in_order_and_is_deterministic():
    a, b, c, d, e = (item(x, relevance=r) for x, r in
                     [("ev_a", .5), ("ev_b", .9), ("ev_c", .9), ("ev_d", .1), ("ev_e", .9)])
    keys = {
        a.id: dict(recency=2, credibility=2, priority=1),
        b.id: dict(recency=2, credibility=2, priority=2),  # same buckets, more relevant than a
        c.id: dict(recency=2, credibility=2, priority=2),  # ties with b: id decides
        d.id: dict(recency=2, credibility=1, priority=1),  # lower credibility bucket
        e.id: dict(recency=1, credibility=2, priority=1),  # lower recency bucket beats credibility
    }
    items = [a, b, c, d, e]
    order = sorted(items, key=lambda i: rank_key(i, **keys[i.id]))
    assert [i.id for i in order] == ["ev_b", "ev_c", "ev_a", "ev_d", "ev_e"]
    again = sorted(reversed(order), key=lambda i: rank_key(i, **keys[i.id]))
    assert [i.id for i in again] == [i.id for i in order]


def test_priority_breaks_a_relevance_tie_with_one_as_most_important():
    x, y = item("ev_x"), item("ev_y")
    assert rank_key(y, recency=1, credibility=1, priority=1) < rank_key(
        x, recency=1, credibility=1, priority=2)


# --- compression ----------------------------------------------------------------------------

def test_compression_cuts_lowest_ranked_whole_items_and_keeps_an_aspect_alive():
    ranked = [item("ev_1", "a1"), item("ev_2", "a1"), item("ev_3", "a1"), item("ev_4", "a2")]
    kept, cut = compress_to_budget(ranked, lambda items: len(items) <= 2)
    assert [i.id for i in kept] == ["ev_1", "ev_4"]  # a2's only item survives a higher rank cut
    assert [i.id for i in cut] == ["ev_3", "ev_2"]
    assert all(i.statement == i.id * 10 for i in kept)  # whole items, not shortened


def test_compression_cuts_a_lone_aspect_item_when_nothing_else_can_go():
    kept, cut = compress_to_budget([item("ev_1", "a1"), item("ev_2", "a2")],
                                   lambda i: len(i) <= 1)
    assert [i.id for i in kept] == ["ev_1"] and [i.id for i in cut] == ["ev_2"]


def test_nothing_is_cut_when_it_already_fits():
    ranked = [item("ev_1"), item("ev_2")]
    assert compress_to_budget(ranked, lambda i: True) == (ranked, [])


def test_named_entities_skip_the_first_word_of_a_sentence_and_possessives():
    from insightforge_agent.domain.passages import named_entities_in

    assert named_entities_in("BYD sold cars. Tesla's Model 3 cost less in China.") == {
        "Model", "China"}
    assert named_entities_in("sales rose at Tesla and BYD") == {"Tesla", "BYD"}


def test_a_source_cites_another_by_address_or_domain():
    from insightforge_agent.domain.evidence import cites, domain_of

    assert domain_of("https://www.Example.com/a") == "example.com"
    assert cites(["See https://example.com/a for more"], "https://example.com/a")
    assert cites(["reported by example.com"], "https://www.example.com/other")
    assert not cites(["nothing here"], "https://example.com/a")
