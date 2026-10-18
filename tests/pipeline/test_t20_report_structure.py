"""T20 B1: the Writer stage files Claims by section, covers every Evidence section, and holds the
Executive Summary to a word limit and a lexical grounding check (S1.1, S11.1, S11.4, S11.7)."""

import json
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportDraft,
    ReportSection,
    section_confidence,
)
from insightforge_agent.pipeline.errors import RunFailed
from insightforge_agent.pipeline.write import (
    SUMMARY_MAX_WORDS,
    place_claims,
    summary_problems,
    unsupported_summary_sentences,
    write_report,
)
from tests.scripted import ScriptedChat

REF = PassageRef(observation_id="obs_1", index=0)
SALES = "BYD sold 4.27 million vehicles in 2025."
PRICE = "BYD cut the Seagull price to 9,700 dollars."
CELLS = "LFP cells cost 56 dollars per kWh."


def item(eid, aspect, statement, confidence="MEDIUM", entity="BYD"):
    return EvidenceItem(id=eid, subtask_id="s1", aspect_id=aspect, statement=statement,
                        confidence=confidence, supporting=[REF], primary=REF, entity=entity)


BUNDLE = EvidenceBundle(sections=[
    EvidenceSection(aspect_id="a1", heading="Sales", confidence="HIGH",
                    items=[item("ev_1", "a1", SALES, "HIGH")]),
    EvidenceSection(aspect_id="a2", heading="Pricing", confidence="MEDIUM",
                    items=[item("ev_2", "a2", PRICE)]),
    EvidenceSection(aspect_id="a3", heading="Batteries", confidence="LOW",
                    items=[item("ev_3", "a3", CELLS, "LOW", entity="LFP")]),
])
HIST = HistoricalItem(id="hist_1", subtask_id="m1", aspect_id="a9", heading="Earlier outlook",
                      statement="BYD planned a plant in Hungary.", entity="BYD",
                      report_id="rep_old", run_id="run_old", report_date=date(2026, 1, 5),
                      primary=REF)
GOOD_SUMMARY = "BYD sold 4.27 million vehicles in 2025. LFP cells cost 56 dollars per kWh."


def claim(text, eid, historical=False):
    return {"text": text, "evidence_id": eid, "historical": historical}


def reply(sections, summary=GOOD_SUMMARY) -> AIMessage:
    return AIMessage(content=json.dumps({"title": "EV report", "summary": summary, "sections": [
        {"heading": h, "claims": cs} for h, cs in sections]}))


ALL_PLACED = [("Sales", [claim(SALES, "ev_1")]), ("Pricing", [claim(PRICE, "ev_2")]),
              ("Batteries", [claim(CELLS, "ev_3")])]


def writer(*replies) -> ScriptedChat:
    return ScriptedChat(script=list(replies))


def write(deps, historical=None):
    return write_report(deps, "alice", "run_1", "EV market", BUNDLE, ["gap"], historical)


def logged(deps, type_):
    return [e.payload for e in deps.repos.events.read_after("alice", "run_1", 0)
            if e.type == type_]


def headings(draft):
    return [(s.heading, [c.evidence_id for c in s.claims]) for s in draft.sections]


# --- placement -----------------------------------------------------------------------------

def test_misplaced_current_claims_are_filed_under_their_evidence_section(make_deps):
    model = writer(reply([("Misc", [claim(PRICE, "ev_2"), claim(SALES, "ev_1")]),
                          ("Batteries", [claim(CELLS, "ev_3")])]))
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert len(model.seen) == 1  # placement is fixed in code, not by asking again
    assert headings(draft) == [("Sales", ["ev_1"]), ("Pricing", ["ev_2"]),
                               ("Batteries", ["ev_3"])]
    (placed,) = logged(deps, "writer_claims_placed")
    assert placed["moved"] == [{"evidence_id": "ev_2", "from": "Misc", "to": "Pricing"},
                               {"evidence_id": "ev_1", "from": "Misc", "to": "Sales"}]


def test_no_claim_is_dropped_and_claim_order_is_kept_within_a_section():
    second = "BYD exported 1 million vehicles in 2025."
    bundle = EvidenceBundle(sections=[EvidenceSection(
        aspect_id="a1", heading="Sales", confidence="HIGH",
        items=[item("ev_1", "a1", SALES), item("ev_4", "a1", second)])])
    draft = ReportDraft(title="T", sections=[
        ReportSection(heading="Other", claims=[Claim(text=second, evidence_id="ev_4")]),
        ReportSection(heading="Sales", claims=[Claim(text=SALES, evidence_id="ev_1")])])
    placed, _ = place_claims(draft, bundle)
    assert headings(placed) == [("Sales", ["ev_4", "ev_1"])]


def test_duplicate_headings_are_merged_case_and_spacing_ignored():
    draft = ReportDraft(title="T", sections=[
        ReportSection(heading="sales", claims=[Claim(text=SALES, evidence_id="ev_1")]),
        ReportSection(heading="Pricing", claims=[Claim(text=PRICE, evidence_id="ev_2")]),
        ReportSection(heading=" SALES ", claims=[Claim(text=SALES, evidence_id="ev_1")])])
    placed, moved = place_claims(draft, BUNDLE)
    assert headings(placed) == [("Sales", ["ev_1", "ev_1"]), ("Pricing", ["ev_2"])]
    assert moved == []  # same heading, only spelled differently


def test_two_evidence_sections_with_one_heading_share_one_report_section():
    bundle = EvidenceBundle(sections=[
        EvidenceSection(aspect_id="a1", heading="Market", confidence="HIGH",
                        items=[item("ev_1", "a1", SALES, "HIGH")]),
        EvidenceSection(aspect_id="a2", heading="Market", confidence="LOW",
                        items=[item("ev_2", "a2", PRICE, "LOW")])])
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Market", claims=[
        Claim(text=SALES, evidence_id="ev_1"), Claim(text=PRICE, evidence_id="ev_2")])])
    placed, _ = place_claims(draft, bundle)
    assert headings(placed) == [("Market", ["ev_1", "ev_2"])]
    assert section_confidence(placed, bundle) == ["LOW"]  # the lower of the two, never raised


def test_historical_claims_and_unknown_ids_are_left_for_the_rules_to_judge(make_deps):
    hist = claim("BYD planned a plant in Hungary (reported 2026-01-05).", "hist_1", True)
    model = writer(reply([*ALL_PLACED, ("Earlier outlook", [hist])]))
    deps = make_deps(writer_model=model)
    draft = write(deps, HistoricalContext(items=[HIST]))
    assert headings(draft)[-1] == ("Earlier outlook", ["hist_1"])
    assert section_confidence(draft, BUNDLE) == ["HIGH", "MEDIUM", "LOW", None]

    misplaced = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=hist["text"], evidence_id="hist_1", historical=True),
        Claim(text=SALES, evidence_id="ev_nope")])])
    placed, moved = place_claims(misplaced, BUNDLE)
    assert headings(placed) == [("Sales", ["hist_1", "ev_nope"])] and moved == []


# --- coverage ------------------------------------------------------------------------------

def test_an_omitted_evidence_section_is_retried(make_deps):
    model = writer(reply(ALL_PLACED[:2]), reply(ALL_PLACED))
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert [s.heading for s in draft.sections] == ["Sales", "Pricing", "Batteries"]
    retry = str(model.seen[1][-1].content)
    assert "the evidence section headed 'Batteries' has no claims" in retry and "ev_3" in retry


def test_a_section_still_omitted_after_retries_is_filled_from_its_evidence(make_deps):
    model = writer(*[reply(ALL_PLACED[:2])] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert len(model.seen) == 3
    assert headings(draft) == [("Sales", ["ev_1"]), ("Pricing", ["ev_2"]),
                               ("Batteries", ["ev_3"])]
    assert draft.sections[2].claims[0].text == CELLS  # the item's own statement, verbatim
    assert draft.gaps == ["gap"]
    (repaired,) = logged(deps, "writer_repaired")
    assert repaired["filled_sections"] == ["Batteries"]


def test_claim_rule_violations_still_fail_the_run(make_deps):
    bad = reply([*ALL_PLACED[:2], ("Batteries", [claim("Tesla cells cost 56 dollars.", "ev_3")])])
    deps = make_deps(writer_model=writer(bad, bad, bad))
    with pytest.raises(RunFailed):
        write(deps)


def test_an_earlier_draft_with_passing_claims_is_kept_over_a_later_broken_one(make_deps):
    long_summary = " ".join(["BYD"] * (SUMMARY_MAX_WORDS + 1))
    broken = reply([("Sales", [claim(SALES, "ev_nope")])])
    model = writer(reply(ALL_PLACED, summary=long_summary), broken, broken)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert headings(draft) == [("Sales", ["ev_1"]), ("Pricing", ["ev_2"]),
                               ("Batteries", ["ev_3"])]
    assert draft.summary == long_summary  # over the limit, but nothing unsupported


# --- the Executive Summary -----------------------------------------------------------------

def test_a_summary_with_an_invented_number_or_name_is_retried(make_deps):
    invented = "BYD sold 5 million vehicles in 2025. Tesla lost ground."
    model = writer(reply(ALL_PLACED, summary=invented), reply(ALL_PLACED))
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert draft.summary == GOOD_SUMMARY
    retry = str(model.seen[1][-1].content)
    assert "values ['5']" in retry and "names ['Tesla']" in retry


def test_unsupported_summary_sentences_are_removed_when_retries_run_out(make_deps):
    invented = "BYD sold 4.27 million vehicles in 2025. Rivals earned 80 billion dollars."
    model = writer(*[reply(ALL_PLACED, summary=invented)] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert draft.summary == "BYD sold 4.27 million vehicles in 2025."
    (repaired,) = logged(deps, "writer_repaired")
    assert repaired["removed_summary_sentences"] == ["Rivals earned 80 billion dollars."]
    assert repaired["summary_rebuilt"] is False


def test_a_wholly_unsupported_summary_is_rebuilt_from_the_claims(make_deps):
    model = writer(*[reply(ALL_PLACED, summary="Tesla leads with 60 percent.")] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert draft.summary == f"{SALES} {PRICE} {CELLS}"
    assert unsupported_summary_sentences(draft.summary, BUNDLE) == []
    assert logged(deps, "writer_repaired")[0]["summary_rebuilt"] is True


def test_an_empty_summary_is_retried_then_rebuilt(make_deps):
    model = writer(*[reply(ALL_PLACED, summary="")] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert "the summary is empty" in str(model.seen[1][-1].content)
    assert draft.summary == f"{SALES} {PRICE} {CELLS}"


def test_an_over_long_summary_is_retried_but_never_fails_the_run(make_deps):
    long_summary = " ".join([SALES] * 30)  # 210 words, every one supported
    model = writer(*[reply(ALL_PLACED, summary=long_summary)] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert "it may have at most 150" in str(model.seen[1][-1].content)
    assert draft.summary == long_summary
    (repaired,) = logged(deps, "writer_repaired")
    assert repaired["summary_over_limit"] is True and repaired["summary_words"] == 210


def test_the_word_limit_is_inclusive():
    exact = ReportDraft(title="T", summary=" ".join(["BYD"] * SUMMARY_MAX_WORDS), sections=[])
    over = exact.model_copy(update={"summary": exact.summary + " BYD"})
    assert summary_problems(exact, BUNDLE) == []
    assert summary_problems(over, BUNDLE) == ["the summary has 151 words; it may have at most 150"]


@pytest.mark.parametrize("sentence", [
    "Overall, BYD sold 4.27 million vehicles in 2025.",
    "However, LFP cells cost 56 dollars per kWh.",
    "In summary, Seagull pricing matters.",
    "Pricing pressure is rising.",  # a heading word
    "The EV market is moving.",  # a Brief word
    "BYD's Seagull costs 9,700 dollars.",  # a possessive of a known name
])
def test_common_openers_headings_brief_words_and_possessives_are_not_flagged(sentence):
    assert unsupported_summary_sentences(sentence, BUNDLE, brief="EV market") == []


@pytest.mark.parametrize("sentence, why", [
    ("Tesla sold 4.27 million vehicles.", "names ['Tesla']"),
    ("Sales rose against Tesla.", "names ['Tesla']"),
    ("BYD sold 4.3 million vehicles.", "values ['4.3']"),
    ("BYD will sell 5 million in 2030.", "values ['2030', '5']"),
])
def test_invented_names_and_numbers_are_flagged(sentence, why):
    ((_, reason),) = unsupported_summary_sentences(sentence, BUNDLE)
    assert reason == why


def test_a_summary_may_use_historical_background():
    sentence = "BYD planned a plant in Hungary."
    assert unsupported_summary_sentences(sentence, BUNDLE)  # not current evidence
    assert unsupported_summary_sentences(sentence, BUNDLE, HistoricalContext(items=[HIST])) == []


# --- Confidence, derived on read -----------------------------------------------------------

def test_a_legacy_report_with_claims_under_an_invented_heading_still_gets_a_label():
    legacy = ReportDraft(title="T", sections=[
        ReportSection(heading="Findings", claims=[Claim(text=SALES, evidence_id="ev_1"),
                                                  Claim(text=PRICE, evidence_id="ev_2")]),
        ReportSection(heading="Empty", claims=[])])
    assert section_confidence(legacy, BUNDLE) == ["MEDIUM", None]
