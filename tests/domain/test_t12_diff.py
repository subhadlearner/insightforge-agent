"""T12 CP2: deterministic comparison against established per-Finding state (ADR-0004)."""

from datetime import timedelta

from insightforge_agent.domain.diff import EstablishedFinding, compare, establish
from insightforge_agent.domain.findings import DEFAULT_REGISTRY
from tests.domain.t12_builders import T, finding

REG = DEFAULT_REGISTRY


def state(*findings, **kw):
    return {f.key: EstablishedFinding(finding=f, **kw) for f in findings}


def outcomes(result):
    return [(e.outcome, e.finding.object if e.finding else e.previous.object)
            for e in result.entries]


def run(n, **kw):
    """A Finding from Run n."""
    return finding(run_id=f"r{n}", report_id=f"rep{n}", **kw)


def test_seed_transition_establishes_dated_findings_only():
    result = establish([
        run(1, value="$100"),
        run(1, entity="Globex", value="$5", period="UNKNOWN"),
        run(1, predicate="offers", value="Model X"),
    ], REG)
    assert sorted(outcomes(result)) == [("added", "$100"), ("added", "Model X"),
                                        ("ambiguous", "$5")]
    assert {u.finding.object for u in result.updates.values()} == {"$100", "Model X"}


def test_reconfirmed_resets_the_count_and_refreshes_provenance_and_period():
    old = run(1, value="$100", period="2030-01")
    result = compare(state(old, not_reconfirmed=2), [run(2, value="100 dollars",
                                                         period="2030-02")], REG)
    assert outcomes(result) == [("reconfirmed", "100 dollars")]
    new = result.updates[old.key]
    assert new.not_reconfirmed == 0 and not new.unconfirmed
    assert new.finding.provenance.report_id == "rep2" and new.finding.as_of_period == "2030-02"
    assert not result.material


def test_single_valued_change_beyond_tolerance_is_material_and_keeps_previous():
    old = run(1, value="$100", period="2030-01")
    result = compare(state(old), [run(2, value="$130", period="2030-02")], REG)
    [entry] = result.entries
    assert entry.outcome == "changed" and entry.material
    assert entry.previous.provenance.report_id == "rep1"
    assert result.updates[old.key].finding.object == "$130"


def test_absence_is_never_deletion_and_k_marks_unconfirmed_without_alert():
    old = run(1, value="$100")
    established = state(old)
    counts = []
    for n in range(2, 6):
        result = compare(established, [run(n, entity="Globex", value="$1")], REG)
        entry = next(e for e in result.entries if e.key == old.key)
        assert entry.outcome == "not_reconfirmed"
        counts.append((entry.not_reconfirmed, entry.unconfirmed))
        assert result.updates[old.key].finding == old  # value and provenance kept
        established = {old.key: result.updates[old.key]}
        assert not any(e.material and e.key == old.key for e in result.entries)
    assert counts == [(1, False), (2, False), (3, True), (4, True)]
    back = compare(established, [run(9, value="$100")], REG)
    assert outcomes(back) == [("reconfirmed", "$100")]
    assert back.updates[old.key].not_reconfirmed == 0
    assert not back.updates[old.key].unconfirmed


def test_older_value_never_replaces_a_newer_established_one():
    old = run(1, value="$130", period="2030-02")
    result = compare(state(old, not_reconfirmed=1), [run(2, value="$100", period="2029")], REG)
    assert [e.outcome for e in result.entries] == ["older_ignored", "not_reconfirmed"]
    assert result.updates[old.key].finding == old
    assert result.updates[old.key].not_reconfirmed == 2
    assert not result.material


def test_unknown_period_is_flagged_and_neither_change_nor_contradiction():
    old = run(1, value="$100")
    result = compare(state(old), [run(2, value="$999", period="UNKNOWN")], REG)
    assert [e.outcome for e in result.entries] == ["ambiguous", "not_reconfirmed"]
    assert result.entries[0].reason == "UNKNOWN As-of period"
    assert not result.material
    new_only = compare({}, [run(2, value="$5", period="UNKNOWN")], REG)
    assert [e.outcome for e in new_only.entries] == ["ambiguous"] and not new_only.updates


def test_same_period_different_value_is_ambiguous_not_a_change():
    old = run(1, value="$100", period="2030")
    result = compare(state(old), [run(2, value="$110", period="2030")], REG)
    assert result.entries[0].outcome == "ambiguous" and not result.material


def test_derived_periods_on_the_same_day_are_ordered_by_observation_time():
    old = run(1, value="$100", period="2030-03-01", derived_from="observation_date")
    later = run(2, value="$130", period="2030-03-01", derived_from="observation_date",
                observed=T + timedelta(hours=6))
    assert compare(state(old), [later], REG).entries[0].outcome == "changed"
    earlier = run(2, value="$90", period="2030-03-01", derived_from="observation_date",
                  observed=T - timedelta(hours=6))
    assert compare(state(old), [earlier], REG).entries[0].outcome == "older_ignored"


def test_conflicting_values_within_one_run_are_ambiguous():
    old = run(1, value="$100", period="2030-01")
    two = [run(2, value="$110", period="2030-02", evidence_id="a"),
           run(2, value="$120", period="2030-02", evidence_id="b")]
    result = compare(state(old), two, REG)
    assert [e.outcome for e in result.entries] == ["ambiguous", "ambiguous", "not_reconfirmed"]
    flagged = compare(state(old), [run(2, value="$130", period="2030-02", conflicting=True)],
                      REG)
    assert flagged.entries[0].outcome == "ambiguous" and not flagged.material


def test_the_newest_value_in_a_run_wins_and_older_ones_are_kept_for_audit():
    old = run(1, value="$100", period="2030-01")
    result = compare(state(old), [run(2, value="$90", period="2029", evidence_id="a"),
                                  run(2, value="$130", period="2030-02", evidence_id="b")], REG)
    assert sorted(e.outcome for e in result.entries) == ["changed", "older_ignored"]


def test_explicit_negated_finding_is_no_longer_true():
    offer = run(1, predicate="offers", value="Model X")
    negated = run(2, predicate="discontinued", value="Model X", polarity="negated",
                  period="2030-03")
    result = compare(state(offer), [negated], REG)
    assert outcomes(result) == [("no_longer_true", "Model X")] and result.material
    assert result.updates[offer.key].finding.polarity == "negated"
    again = compare({offer.key: result.updates[offer.key]},
                    [run(3, predicate="offers", value="Model X", period="2030-04")], REG)
    assert outcomes(again) == [("added", "Model X")]


def test_opposite_polarity_for_the_same_period_is_ambiguous_not_material():
    offer = run(1, predicate="offers", value="Model X", period="2030-02")
    denied = run(2, predicate="offers", value="Model X", polarity="negated", period="2030-02")
    result = compare(state(offer), [denied], REG)
    assert [e.outcome for e in result.entries] == ["ambiguous", "not_reconfirmed"]
    assert not result.material


def test_negating_a_different_single_value_is_not_a_change():
    old = run(1, value="$100", period="2030-01")
    result = compare(state(old), [run(2, value="$130", period="2030-02", polarity="negated")],
                     REG)
    assert [e.outcome for e in result.entries] == ["not_reconfirmed"]
    assert "different value" in result.entries[0].reason


def test_negated_finding_older_than_established_is_ignored():
    offer = run(1, predicate="offers", value="Model X", period="2030-02")
    stale = run(2, predicate="offers", value="Model X", polarity="negated", period="2029")
    result = compare(state(offer), [stale], REG)
    assert result.entries[0].outcome == "older_ignored" and not result.material


def test_multi_valued_members_are_independent_and_absence_is_not_removal():
    a = run(1, predicate="offers", value="Model A")
    b = run(1, predicate="offers", value="Model B")
    result = compare(state(a, b), [run(2, predicate="offers", value="Model A"),
                                   run(2, predicate="offers", value="Model C")], REG)
    assert sorted(outcomes(result)) == [("added", "Model C"), ("not_reconfirmed", "Model B"),
                                        ("reconfirmed", "Model A")]


def test_net_change_after_failed_diffs_compares_with_unchanged_baseline():
    """A failed Diff never wrote state, so the next Diff sees the net change: A -> C."""
    base = state(run(1, value="$100", period="2030-01"))
    # Run 2 ($110) failed permanently: nothing applied. Run 3 says $120.
    result = compare(base, [run(3, value="$120", period="2030-03")], REG)
    [entry] = result.entries
    assert (entry.outcome, entry.previous.object, entry.finding.object) == \
        ("changed", "$100", "$120")
    # And a value that went away and came back is no change at all.
    assert compare(base, [run(3, value="$100", period="2030-03")], REG).entries[0].outcome \
        == "reconfirmed"
