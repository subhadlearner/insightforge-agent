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
from tests.pipeline.test_cited_passage_and_publication_checks import judge
from tests.pipeline.test_writer_and_fact_check import evidence
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


# ==== The comparison the Brief asked for (same Run) =========================================
# The Writer wrote "BYD's 2025 passenger BEV sales (2,256,714) exceeded Tesla's 2025 deliveries
# (1,636,129) ...", citing both figures. The joint judge said SUPPORTED, quoting "... over a full
# year"; the page's sentence ends "over a full year<sup>3</sup>.", stored as "year3.", so the
# exact-words check refused the quote (VERIFICATION_ERROR) and the Report stated no comparison.
# A footnote number glued to a plain word is now the form difference `provenance._same` already
# allows; the quoted word itself must still match.

BYD = ("In 2025, BYD sold 2,256,714 passenger BEVs, beating Tesla’s 1,636,129 deliveries for "
       "the first time to become the world’s largest seller of battery-electric cars over a "
       "full year3.")
TESLA = "Tesla reported 1,636,129 deliveries in full-year 2025."
COMPARISON = ("BYD's 2025 passenger BEV sales (2,256,714) exceeded Tesla's 2025 deliveries "
              "(1,636,129), making BYD the world's largest seller of battery-electric cars.")


def comparing(verdict: str, quote: str):
    """A light model whose joint judgment is `verdict`, quoting `quote` from passage 1 and the
    Tesla sentence from passage 2; single-Passage judgments follow `judge()`'s default."""
    from langchain_core.messages import SystemMessage

    single = judge()

    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        if system.startswith("You check whether numbered passages, taken together"):
            return AIMessage(content=json.dumps({"verdict": verdict, "reason": "r", "parts": [
                {"part": COMPARISON, "excerpts": [{"passage": 1, "text": quote},
                                                  {"passage": 2, "text": TESLA}]}]
                if verdict == "SUPPORTED" else []}))
        return single._generate(messages).generations[0].message
    return ScriptedChat(reply=reply)


def write_comparison(make_deps, model):
    writer = ScriptedChat(reply=lambda m: AIMessage(content=json.dumps(
        {"title": "T", "summary": TESLA, "sections": [{"heading": "Sales", "claims": [
            {"text": COMPARISON, "evidence_id": "ev_0", "premises": ["ev_1"]},
            {"text": TESLA, "evidence_id": "ev_1"}]}]})))
    deps = make_deps(light_model=model, writer_model=writer)
    _, bundle = evidence(deps, [BYD, TESLA])
    draft = write_report(deps, "alice", "run_1", "Compare BYD and Tesla", bundle, [])
    return deps, [(c.text, c.premises) for s in draft.sections for c in s.claims]


def test_a_supported_comparison_quoting_a_footnoted_sentence_is_published_with_both_citations(
        make_deps):
    quote = ("In 2025, BYD sold 2,256,714 passenger BEVs, beating Tesla’s 1,636,129 "
             "deliveries for the first time to become the world’s largest seller of "
             "battery-electric cars over a full year")
    deps, claims = write_comparison(make_deps, comparing("SUPPORTED", quote))
    assert (COMPARISON, ["ev_1"]) in claims
    assert not [e for e in deps.repos.events.read_after("alice", "run_1")
                if e.type == "analytical_claim_rejected"]


def test_an_unsupported_or_misquoted_comparison_is_still_rejected(make_deps):
    quote = ("BYD sold 2,256,714 passenger BEVs, beating Tesla’s 1,636,129 deliveries over "
             "a full year")
    for name, model, verdict in (
            ("judged insufficient", comparing("INSUFFICIENT_EVIDENCE", quote), "INSUFFICIENT_EVIDENCE"),
            # a quote whose words are not the passage's ("over a full year" is, "beating ...
            # over" skips words): still not exact words of the unit
            ("misquoted", comparing("SUPPORTED", quote), "VERIFICATION_ERROR")):
        deps, claims = write_comparison(make_deps, model)
        assert COMPARISON not in [t for t, _ in claims], name
        (rejected,) = [e.payload for e in deps.repos.events.read_after("alice", "run_1")
                       if e.type == "analytical_claim_rejected"]
        assert [r["verdict"] for r in rejected["rejected"]] == [verdict], name


def test_a_footnote_number_is_tolerated_only_on_a_plain_word_of_the_text():
    from insightforge_agent.domain.support import find_excerpt

    assert find_excerpt("over a full year", BYD) is not None
    assert find_excerpt("over a full year3", BYD) is not None  # the text's own form
    assert find_excerpt("over a full years", BYD) is None  # no plural or other change
    assert find_excerpt("the B30A chip", "the B30A1 chip") is None  # never a model number
    assert find_excerpt("H20 sales", "H200 sales") is None
