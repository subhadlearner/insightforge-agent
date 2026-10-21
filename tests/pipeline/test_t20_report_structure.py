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
    NO_SUMMARY,
    SUMMARY_MAX_WORDS,
    fit_summary,
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
    assert section_confidence(draft, BUNDLE, HistoricalContext(items=[HIST])) == [
        "HIGH", "MEDIUM", "LOW", "BACKGROUND"]

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


def test_a_missing_section_of_several_items_is_filled_with_one_best_ranked_claim(make_deps):
    second = "LFP cells cost 60 dollars per kWh in 2024."
    bundle = BUNDLE.model_copy(update={"sections": [*BUNDLE.sections[:2], EvidenceSection(
        aspect_id="a3", heading="Batteries", confidence="LOW",
        items=[item("ev_3", "a3", CELLS, "LOW", entity="LFP"),
               item("ev_5", "a3", second, "LOW", entity="LFP")])]})
    deps = make_deps(writer_model=writer(*[reply(ALL_PLACED[:2])] * 3))
    draft = write_report(deps, "alice", "run_1", "EV market", bundle, [])
    (batteries,) = [s for s in draft.sections if s.heading == "Batteries"]
    assert [(c.text, c.evidence_id) for c in batteries.claims] == [(CELLS, "ev_3")]
    assert sum(len(s.claims) for s in draft.sections) == 3  # one Claim added, not two


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
    # T18 Stage C changed this: an ungrounded Claim with a valid citation now goes to the semantic
    # check (tests/pipeline/test_t18_writer_fallback.py). An invented id still fails.
    bad = reply([*ALL_PLACED[:2], ("Batteries", [claim("Tesla cells cost 56 dollars.", "ev_9")])])
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
    # One 151-word sentence: no whole sentence fits, so the summary is built from the Claims.
    assert draft.summary == f"{SALES} {PRICE} {CELLS}"


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


def test_an_over_long_summary_is_retried_then_cut_to_whole_sentences_never_published(make_deps):
    long_summary = " ".join([SALES] * 30)  # 210 words, every one supported
    model = writer(*[reply(ALL_PLACED, summary=long_summary)] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert len(model.seen) == 3  # no model call beyond the existing retries
    assert "it may have at most 150" in str(model.seen[1][-1].content)
    assert draft.summary == " ".join([SALES] * 21)  # 147 words: the 22nd sentence would pass 150
    (repaired,) = logged(deps, "writer_repaired")
    assert repaired["summary_shortened"] == "sentences" and repaired["summary_words"] == 147


def test_a_summary_of_exactly_150_words_is_published_untouched(make_deps):
    exact = " ".join([SALES] * 20) + " " + " ".join(["BYD"] * 9) + " BYD."  # 140 + 10 words
    assert len(exact.split()) == SUMMARY_MAX_WORDS
    model = writer(reply(ALL_PLACED, summary=exact))
    deps = make_deps(writer_model=model)
    assert write(deps).summary == exact and len(model.seen) == 1
    assert logged(deps, "writer_repaired") == []


def test_a_151_word_summary_is_retried(make_deps):
    over = " ".join([SALES] * 20) + " " + " ".join(["BYD"] * 10) + " BYD."
    assert len(over.split()) == SUMMARY_MAX_WORDS + 1
    model = writer(reply(ALL_PLACED, summary=over), reply(ALL_PLACED))
    deps = make_deps(writer_model=model)
    assert write(deps).summary == GOOD_SUMMARY
    assert "the summary has 151 words" in str(model.seen[1][-1].content)


DRAFT = ReportDraft(title="T", sections=[
    ReportSection(heading="Sales", claims=[Claim(text=SALES, evidence_id="ev_1")]),
    ReportSection(heading="Pricing", claims=[Claim(text=PRICE, evidence_id="ev_2")])])


@pytest.mark.parametrize("sentences, expected, how", [
    ([SALES, PRICE], f"{SALES} {PRICE}", None),
    ([SALES] * 22, " ".join([SALES] * 21), "sentences"),
    ([" ".join(["BYD"] * 151) + "."], f"{SALES} {PRICE}", "claims"),
    ([], f"{SALES} {PRICE}", "claims"),
    (["", "  "], f"{SALES} {PRICE}", "claims"),
])
def test_fit_summary_keeps_whole_supported_text_within_the_limit(sentences, expected, how):
    summary, shortened = fit_summary(sentences, DRAFT)
    assert (summary, shortened) == (expected, how)
    assert len(summary.split()) <= SUMMARY_MAX_WORDS


def test_nothing_is_ever_cut_mid_claim_a_finding_free_sentence_stands_in():
    long_claim = " ".join(["BYD"] * 200) + " did not grow."  # cut, it would lose "not"
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=long_claim, evidence_id="ev_1")])])
    assert fit_summary([long_claim], draft) == (NO_SUMMARY, "placeholder")
    assert len(NO_SUMMARY.split()) <= SUMMARY_MAX_WORDS
    assert unsupported_summary_sentences(NO_SUMMARY, BUNDLE)  # it is no finding: never grounded


BLANK = [("Sales", [claim("", "ev_1")]), ("Pricing", [claim(" 	", "ev_2")]),
         ("Batteries", [claim(CELLS, "ev_3")])]


def test_a_blank_claim_is_a_violation_even_with_a_valid_evidence_id(make_deps):
    model = writer(reply(BLANK), reply(ALL_PLACED))
    deps = make_deps(writer_model=model)
    draft = write(deps)
    assert [c.text for s in draft.sections for c in s.claims] == [SALES, PRICE, CELLS]
    retry = str(model.seen[1][-1].content)
    assert "the claim citing 'ev_1' under 'Sales' has no text" in retry
    assert "the claim citing 'ev_2' under 'Pricing' has no text" in retry


def test_blank_claims_never_cover_a_section_or_reach_repair(make_deps):
    deps = make_deps(writer_model=writer(*[reply(BLANK, summary="")] * 3))
    with pytest.raises(RunFailed):  # never an empty substantive Report
        write(deps)
    assert logged(deps, "writer_repaired") == []


def test_a_background_only_report_summary_falls_back_to_its_historical_claims():
    dated = "BYD planned a plant in Hungary (reported 2026-01-05)."
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Earlier outlook", claims=[
        Claim(text=dated, evidence_id="hist_1", historical=True)])])
    assert fit_summary([], draft) == (dated, "claims")


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


HISTORY = HistoricalContext(items=[HIST])
UNDATED = "BYD planned a plant in Hungary."
DATED = "BYD planned a plant in Hungary (reported 2026-01-05)."


def test_undated_earlier_report_background_is_not_a_current_conclusion():
    ((_, why),) = unsupported_summary_sentences(UNDATED, BUNDLE, HISTORY)
    assert why == "undated background"
    problems = summary_problems(ReportDraft(title="T", summary=UNDATED, sections=[]),
                                BUNDLE, HISTORY)
    assert problems == [f"summary sentence {UNDATED!r} presents earlier-Report background as "
                        "current; show its report date inline as (reported YYYY-MM-DD), as its "
                        "historical claim does"]


@pytest.mark.parametrize("sentence", [
    DATED, "As reported on 2026-01-05, BYD planned a plant in Hungary.",
    # mixed: a current finding and dated background in one sentence
    "BYD sold 4.27 million vehicles in 2025 and planned a plant in Hungary (reported 2026-01-05).",
])
def test_dated_background_may_appear_in_the_summary(sentence):
    assert unsupported_summary_sentences(sentence, BUNDLE, HISTORY) == []


def test_background_dated_with_the_wrong_report_is_not_supported():
    wrong = "BYD planned a plant in Hungary (reported 2025-01-05)."
    ((_, why),) = unsupported_summary_sentences(wrong, BUNDLE, HISTORY)
    # An unrecognised date is no annotation: it is substance, and its figures are unsupported.
    assert why == "values ['01', '05'] and names ['Hungary']"


def test_without_historical_context_background_names_are_simply_unsupported():
    ((_, why),) = unsupported_summary_sentences(UNDATED, BUNDLE)
    assert why == "names ['Hungary']"


def test_a_mixed_summary_keeps_current_and_dated_sentences_and_drops_undated_background(make_deps):
    hist = claim(DATED, "hist_1", True)
    summary = f"{SALES} {UNDATED} {DATED}"
    model = writer(*[reply([*ALL_PLACED, ("Earlier outlook", [hist])], summary=summary)] * 3)
    deps = make_deps(writer_model=model)
    draft = write(deps, HISTORY)
    assert draft.summary == f"{SALES} {DATED}"
    assert logged(deps, "writer_repaired")[0]["removed_summary_sentences"] == [UNDATED]


def test_a_background_only_report_keeps_its_summary_dated(make_deps):
    hist = claim(DATED, "hist_1", True)
    empty = EvidenceBundle(sections=[])
    for summary, expected in [(UNDATED, DATED), (DATED, DATED)]:
        deps = make_deps(writer_model=writer(
            *[reply([("Earlier outlook", [hist])], summary=summary)] * 3))
        draft = write_report(deps, "alice", "run_1", "EV market", empty, [], HISTORY)
        assert draft.summary == expected  # rebuilt from the dated Historical Claim


# --- Confidence, derived on read -----------------------------------------------------------

def test_a_legacy_report_with_claims_under_an_invented_heading_still_gets_a_label():
    legacy = ReportDraft(title="T", sections=[
        ReportSection(heading="Findings", claims=[Claim(text=SALES, evidence_id="ev_1"),
                                                  Claim(text=PRICE, evidence_id="ev_2")]),
        ReportSection(heading="Empty", claims=[])])
    assert section_confidence(legacy, BUNDLE) == ["MEDIUM", None]


def test_an_unresolved_reference_is_never_labelled_background():
    draft = ReportDraft(title="T", sections=[
        ReportSection(heading="Sales", claims=[Claim(text=SALES, evidence_id="ev_gone")]),
        ReportSection(heading="Mixed", claims=[Claim(text=SALES, evidence_id="ev_1"),
                                               Claim(text=SALES, evidence_id="ev_gone")]),
        ReportSection(heading="Outlook", claims=[
            Claim(text=DATED, evidence_id="hist_gone", historical=True)]),
        ReportSection(heading="Earlier outlook", claims=[
            Claim(text=DATED, evidence_id="hist_1", historical=True)])])
    assert section_confidence(draft, BUNDLE, HISTORY) == [None, None, None, "BACKGROUND"]
