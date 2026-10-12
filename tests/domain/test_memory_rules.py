"""T8 (#9): the pure memory rules. Eligibility, which Run summaries the Planner reads, summary
bounds, memory references and the inline report date of a Historical Claim."""

from datetime import UTC, date, datetime, timedelta

import pytest

from insightforge_agent.domain.memory import (
    MAX_ENTITIES,
    MAX_ITEM_CHARS,
    MemoryRef,
    build_run_summary,
    memory_eligible,
    memory_ref_id,
    memory_refs_in,
    planner_summaries,
    render_run_summaries,
    without_report_date,
)
from insightforge_agent.domain.models import Run, RunState, RunSummary

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def run(rid="run_1", owner="alice", state=RunState.COMPLETE, item=None, at=T0) -> Run:
    return Run(id=rid, owner_id=owner, brief="b", state=state, created_at=at,
               watchlist_item_id=item)


def summary(rid="run_1", owner="alice", item=None, at=T0, indexed=True) -> RunSummary:
    return RunSummary(owner_id=owner, run_id=rid, watchlist_item_id=item, created_at=at,
                      brief=f"brief of {rid}", memory_indexed=indexed)


# --- eligibility -------------------------------------------------------------------------------

def test_an_own_complete_finalised_report_is_eligible():
    assert memory_eligible("alice", run(), summary(), None)


@pytest.mark.parametrize("state", [s for s in RunState if s != RunState.COMPLETE])
def test_a_run_that_is_not_complete_never_feeds_memory(state):
    assert not memory_eligible("alice", run(state=state), summary(), None)


def test_another_users_run_is_never_eligible():
    assert not memory_eligible("bob", run(owner="alice"), summary(owner="alice"), None)
    assert not memory_eligible("bob", run(owner="bob"), summary(owner="alice"), None)


def test_a_report_without_its_summary_or_index_is_not_eligible():
    assert not memory_eligible("alice", run(), None, None)
    assert not memory_eligible("alice", run(), summary(indexed=False), None)
    assert not memory_eligible("alice", run(), summary(rid="run_other"), None)


def test_a_watchlist_run_never_uses_reports_of_its_own_item():
    assert not memory_eligible("alice", run(item="w1"), summary(item="w1"), "w1")
    assert memory_eligible("alice", run(item="w2"), summary(item="w2"), "w1")
    assert memory_eligible("alice", run(), summary(), "w1")  # a one-shot Report is fine
    assert memory_eligible("alice", run(item="w1"), summary(item="w1"), None)


# --- Planner summaries ---------------------------------------------------------------------------

def test_the_planner_reads_the_newest_three_one_shot_summaries_of_the_user():
    runs = {f"run_{n}": run(f"run_{n}", at=T0 + timedelta(days=n)) for n in range(5)}
    sums = [summary(f"run_{n}", at=T0 + timedelta(days=n)) for n in range(5)]
    chosen = planner_summaries("alice", sums, runs, None, "run_now")
    assert [s.run_id for s in chosen] == ["run_4", "run_3", "run_2"]


def test_one_shot_summaries_never_include_watchlist_runs_or_other_users():
    runs = {"own": run("own"), "watch": run("watch", item="w1"),
            "bobs": run("bobs", owner="bob"), "failed": run("failed", state=RunState.FAILED)}
    sums = [summary("own"), summary("watch", item="w1"), summary("bobs", owner="bob"),
            summary("failed")]
    assert [s.run_id for s in planner_summaries("alice", sums, runs, None, "x")] == ["own"]


def test_watchlist_summaries_are_for_the_same_item_only():
    runs = {"a": run("a", item="w1"), "b": run("b", item="w2"), "c": run("c")}
    sums = [summary("a", item="w1"), summary("b", item="w2"), summary("c")]
    assert [s.run_id for s in planner_summaries("alice", sums, runs, "w1", "x")] == ["a"]


def test_the_current_run_and_a_summary_without_its_run_are_skipped():
    runs = {"now": run("now")}
    assert planner_summaries("alice", [summary("now"), summary("ghost")], runs, None, "now") == []


def test_rendered_summaries_hold_only_the_summary_fields():
    s = build_run_summary(owner_id="alice", run_id="r", watchlist_item_id=None, created_at=T0,
                          brief="EV market", themes=["Sales"], entities=["BYD"],
                          gaps=["No pricing found."], memory_indexed=True)
    text = render_run_summaries([s])
    assert text.splitlines() == [
        "- Run of 2026-09-01: Brief: EV market", "  Themes: Sales", "  Key entities: BYD",
        "  Gaps & limitations: No pricing found."]


def test_a_summary_is_bounded_and_deduplicated():
    s = build_run_summary(owner_id="alice", run_id="r", watchlist_item_id="w", created_at=T0,
                          brief="x " * 1000, themes=[f"t{n}" for n in range(20)],
                          entities=["BYD", "BYD", ""] + [f"e{n}" for n in range(40)],
                          gaps=["g" * 1000], memory_indexed=False)
    assert len(s.brief) <= 500 and len(s.themes) == 6 and len(s.key_entities) == MAX_ENTITIES
    assert s.key_entities[:2] == ["BYD", "e0"] and len(s.gaps[0]) <= MAX_ITEM_CHARS


# --- references and the inline date -------------------------------------------------------------

def test_memory_references_round_trip_and_are_read_from_a_reply():
    ref = MemoryRef("rep_ab12cd34ef56", 2, 0)
    assert memory_ref_id(ref) == "rep_ab12cd34ef56:s2:c0"
    reply = f"RESULT: found | refs: {memory_ref_id(ref)}, rep_x9:s0:c11, {memory_ref_id(ref)}"
    assert memory_refs_in(reply) == [ref, MemoryRef("rep_x9", 0, 11)]


@pytest.mark.parametrize("claim, expected", [
    ("BYD sold 4.27 million vehicles in 2025 (reported 2026-09-01).",
     "BYD sold 4.27 million vehicles in 2025."),
    ("As reported on 2026-09-01, BYD sold 4.27 million vehicles in 2025.",
     "BYD sold 4.27 million vehicles in 2025."),
    ("BYD sold 4.27 million vehicles in 2025, as reported 2026-09-01.",
     "BYD sold 4.27 million vehicles in 2025, as reported ."),
    ("BYD sold 4.27 million vehicles in 2025.", "BYD sold 4.27 million vehicles in 2025."),
])
def test_the_inline_report_date_is_removed_before_comparing_with_evidence(claim, expected):
    assert without_report_date(claim, date(2026, 9, 1)) == expected
