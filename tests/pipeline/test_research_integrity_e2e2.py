"""Regression test for the second capped live end-to-end Run (run_846d12a50177, 2026-10-21).

Its Report said "Tesla sold 50,300 vehicles in 2025, a 10% decline from 2024." beside Tesla's
1,636,129 global deliveries. The figure is the UK's: Extraction recorded `scope: "UK"`, but the
statement and the sentence it was read from do not say so. The Writer had qualified it ("this
research did not establish its scope"); the semantic Claim check rejected that wording, since the
sentence does not state it, and then restated the item's bare statement in its place. Replayed
offline with the Run's own logged verdicts, the restated figure is published (verification
record §15). A rejected Claim is now omitted with its reason, never replaced by its item's
statement: nothing verifies that the bare statement keeps the scope or context."""

import json

from langchain_core.messages import AIMessage

from insightforge_agent.pipeline.write import OMITTED, write_report
from tests.pipeline.test_extraction_synthesis import world  # noqa: F401
from tests.pipeline.test_research_integrity_review import judge
from tests.pipeline.test_t6_writer_fact_check import evidence
from tests.scripted import ScriptedChat

# The Run's texts: two Evidence statements and the Writer's wording of the second.
GLOBAL = "Tesla reported 1,636,129 deliveries in full-year 2025."
UK = "Tesla sold 50,300 vehicles in 2025, a 10% decline from 2024."
QUALIFIED = ("One item reports Tesla selling 50,300 vehicles in 2025, a 10% decline; this figure "
             "is far below Tesla's global delivery totals and this research did not establish "
             "its scope.")


def test_a_rejected_claim_is_omitted_never_replaced_by_a_statement_without_its_scope(make_deps):
    writer = ScriptedChat(reply=lambda m: AIMessage(content=json.dumps(
        {"title": "T", "summary": GLOBAL, "sections": [{"heading": "Sales", "claims": [
            {"text": GLOBAL, "evidence_id": "ev_0"},
            {"text": QUALIFIED, "evidence_id": "ev_1"}]}]})))
    # The support judgment, as in the Run: the sentence supports its own words only, so the
    # Writer's qualification is INSUFFICIENT_EVIDENCE.
    deps = make_deps(light_model=judge(), writer_model=writer)
    _, bundle = evidence(deps, [GLOBAL, UK])
    draft = write_report(deps, "alice", "run_1", "BYD and Tesla 2025 sales", bundle, [])
    texts = [c.text for s in draft.sections for c in s.claims]
    assert texts == [GLOBAL]
    assert not any("50,300" in t for t in texts)  # neither the qualified nor the bare figure
    (checked,) = [e.payload for e in deps.repos.events.read_after("alice", "run_1")
                  if e.type == "writer_claims_checked"]
    (record,) = checked["rejected"]
    assert (record["evidence_id"], record["verdict"], record["action"], record["reason"]) == (
        "ev_1", "INSUFFICIENT_EVIDENCE", "omitted", OMITTED)
    assert record["original_text"] == QUALIFIED
    assert "claims_removed:1" in draft.quality
    assert not any(q.startswith("claims_restated") for q in draft.quality)
