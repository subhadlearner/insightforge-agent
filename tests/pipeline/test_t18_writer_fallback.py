"""T18 Stage C: the conservative Claim fallback once Writer retries are spent.

The regulatory Run (run_206596674f75) failed because each of its three drafts had one current
Claim adding a name its cited item did not state ("BIS"/"Chinese", "Washington",
"China-specific"), with an over-long summary besides. A current Claim that cites an existing
item now gets that item's exact statement as its text; nothing else is ever guessed at."""

import pytest

from insightforge_agent.domain.contracts import HistoricalContext
from insightforge_agent.pipeline.errors import RunFailed
from tests.pipeline.test_t20_report_structure import (
    ALL_PLACED,
    CELLS,
    HIST,
    PRICE,
    SALES,
    SUMMARY_MAX_WORDS,
    claim,
    logged,
    reply,
    write,
    writer,
)

LONG = " ".join([SALES] * 22)  # 154 words, every one supported: the length error alone

# The three observed patterns, on this bundle's items: an unsupported entity, an unsupported
# caveat, and both with an over-long summary.
ENTITY = "BYD sold 4.27 million vehicles in 2025, per BIS data on Chinese exports."
CAVEAT = "LFP cells cost 56 dollars per kWh; this is not a China-specific figure."


def drafts(sales=SALES, cells=CELLS, summary=None):
    sections = [("Sales", [claim(sales, "ev_1")]), ("Pricing", [claim(PRICE, "ev_2")]),
                ("Batteries", [claim(cells, "ev_3")])]
    return reply(sections, **({"summary": summary} if summary else {}))


@pytest.mark.parametrize("bad, field", [
    (drafts(sales=ENTITY), "ev_1"),
    (drafts(cells=CAVEAT), "ev_3"),
    (drafts(sales=ENTITY, summary=LONG), "ev_1"),
], ids=["unsupported-entity", "unsupported-caveat", "with-over-long-summary"])
def test_an_ungrounded_claim_is_replaced_by_its_items_statement(make_deps, bad, field):
    model = writer(bad, bad, bad)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert len(model.seen) == 3  # the retries ran first; no extra model call
    assert [(c.evidence_id, c.text) for s in draft.sections for c in s.claims] == [
        ("ev_1", SALES), ("ev_2", PRICE), ("ev_3", CELLS)]
    assert [s.heading for s in draft.sections] == ["Sales", "Pricing", "Batteries"]
    (replaced,) = logged(deps, "writer_claims_replaced")
    assert [(r["evidence_id"], r["original_text"]) for r in replaced["replaced"]] == [
        (field, ENTITY if field == "ev_1" else CAVEAT)]
    (repaired,) = logged(deps, "writer_repaired")
    assert repaired["claims_replaced"] == 1
    assert len(draft.summary.split()) <= SUMMARY_MAX_WORDS


def test_the_latest_draft_is_the_one_repaired(make_deps):
    # As in the live Run: each attempt fixes the last problem and makes a new one.
    model = writer(drafts(sales=ENTITY), drafts(cells=CAVEAT), drafts(cells=CAVEAT, summary=LONG))
    deps = make_deps(writer_model=model)
    write(deps)
    (replaced,) = logged(deps, "writer_claims_replaced")
    assert [r["evidence_id"] for r in replaced["replaced"]] == ["ev_3"]


def test_a_draft_whose_claims_pass_is_still_preferred_and_nothing_is_replaced(make_deps):
    model = writer(reply(ALL_PLACED, summary=LONG), drafts(sales=ENTITY), drafts(sales=ENTITY))
    deps = make_deps(writer_model=model)
    write(deps)
    assert logged(deps, "writer_claims_replaced") == []
    assert logged(deps, "writer_repaired")[0]["claims_replaced"] == 0


@pytest.mark.parametrize("sections", [
    # An invented Evidence id: never re-cited to a guessed item.
    [("Sales", [claim(SALES, "ev_invented")]), ("Pricing", [claim(PRICE, "ev_2")]),
     ("Batteries", [claim(CELLS, "ev_3")])],
    # An ungrounded Claim beside an invented id: the whole draft still fails.
    [("Sales", [claim(ENTITY, "ev_1"), claim(SALES, "ev_invented")]),
     ("Pricing", [claim(PRICE, "ev_2")]), ("Batteries", [claim(CELLS, "ev_3")])],
    # A blank Claim is never filled in (T20).
    [("Sales", [claim("", "ev_1")]), ("Pricing", [claim(PRICE, "ev_2")]),
     ("Batteries", [claim(ENTITY, "ev_3")])],
], ids=["invented-id", "ungrounded-beside-invented", "blank"])
def test_anything_but_an_ungrounded_claim_with_a_valid_citation_still_fails(make_deps, sections):
    bad = reply(sections)
    deps = make_deps(writer_model=writer(bad, bad, bad))
    with pytest.raises(RunFailed):
        write(deps)
    assert logged(deps, "writer_claims_replaced") == []


def test_historical_claims_are_never_fabricated_or_rewritten(make_deps):
    historical = HistoricalContext(items=[HIST])
    # The Historical Claim lacks its report date; the fallback must not write one in.
    bad = reply([*ALL_PLACED, ("Earlier outlook", [claim(HIST.statement, "hist_1", True)])])
    deps = make_deps(writer_model=writer(bad, bad, bad))
    with pytest.raises(RunFailed):
        write(deps, historical)
    missing = reply([("Sales", [claim(ENTITY, "ev_1")]), ("Pricing", [claim(PRICE, "ev_2")]),
                     ("Batteries", [claim(CELLS, "ev_3")])])
    deps = make_deps(writer_model=writer(missing, missing, missing))
    with pytest.raises(RunFailed):  # no Historical Claim for the heading: none is invented
        write(deps, historical)
