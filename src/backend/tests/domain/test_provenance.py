"""T18 CS1B: which names an Evidence statement asserts, and whether a Passage states them.

Names asserted by the statement are checked strictly, whole token. A model-written entity label
is a classification, not an assertion: only its words that the statement itself uses as names
(anchors) must be in the Passage. The cases are taken from the T18 evaluation's dropped and
kept Evidence and the Stage B-1 negatives (analysis-v3.1/names_proto_out.txt)."""

import pytest

from insightforge_agent.domain.provenance import (
    asserted_names,
    common_words,
    label_anchors,
    missing_names,
)

CORPUS = common_words([
    "The global market grew and revenue rose. In this report the company says sales fell.",
    "Commercial solar grew in the quarter; commercial installs rose.",
])


def missing(statement, passage, entities=(), entity="", common=CORPUS):
    return missing_names(statement=statement, entities=list(entities), entity=entity,
                         passage_text=passage, common=common).missing


# ---- names asserted by the statement ---------------------------------------------------------

def test_a_sentence_initial_company_is_asserted_even_when_the_entity_fields_are_empty():
    stryker = ("The Q4 2024 reported net sales were $6.4 billion, while the full year reported "
               "net sales were $22.6 billion, an increase of 10.2% from the previous year.")
    assert missing("Medtronic's full year reported net sales were $22.6 billion.", stryker) == [
        "Medtronic"]
    assert missing("Tesla's overseas sales grew 70.7%.", "Overseas sales grew 70.7%.") == ["Tesla"]


def test_ordinary_openers_and_common_words_are_not_names():
    assert asserted_names("The market grew. In 2025 revenue rose.", CORPUS) == set()
    assert asserted_names("Global sales fell.", CORPUS) == set()  # 'global' is a common word
    assert asserted_names("Commercial solar grew 11% in Q4 2025.", CORPUS) == {"Q4"}


def test_name_like_openers_are_names_whatever_the_corpus():
    assert asserted_names("BYD sold 4.6 million cars.", CORPUS) == {"BYD"}
    assert asserted_names("H20 sales stopped.", CORPUS) == {"H20"}
    assert asserted_names("iPhone sales rose.", CORPUS) == {"iPhone"}


def test_units_are_never_names():
    assert asserted_names("India deployed 9,255 MW of solar capacity in 2017.", CORPUS) == {
        "India"}


# ---- matching, whole token -------------------------------------------------------------------

@pytest.mark.parametrize("statement, passage", [
    ("NVIDIA committed $500 billion to US AI infrastructure.",
     "NVIDIA committed $500 billion to AI infrastructure in the U.S. this year."),
    ("AMD's GAAP gross margin was 40%.", "On a non-GAAP basis AMD gross margin was 43%; GAAP 40%."),
    ("BYD's passenger BEV sales beat Tesla.", "BYD sold 2,256,714 passenger BEVs, beating Tesla's."),
    ("BYD became the biggest seller, ahead of Tesla.", "BYD became the biggest seller, ahead of Tesla3."),
    ("It is 12 times faster than the NVIDIA H20.", "more than 12 times faster than the NVIDIA H20.14"),
    ("Azure generated $75 billion in Microsoft's fiscal year 2025, up 34%.",
     "Azure generated $75 billion in Microsoft’s fiscal year 2025, up 34%."),
], ids=["dotted acronym", "hyphen part", "plural", "footnote", "dotted footnote", "possessive"])
def test_a_name_written_differently_only_in_form_is_found(statement, passage):
    assert missing(statement, passage) == []


@pytest.mark.parametrize("statement, passage, absent", [
    ("H200 sales were licensed.", "H20 sales were licensed.", ["H200"]),
    ("H20 sales were licensed.", "H200 sales were licensed.", ["H20"]),
    ("The A10 was banned.", "The A100 was banned.", ["A10"]),
    ("The chip beat the B30A.", "The chip beat the B30A1.", ["B30A"]),
], ids=["H200 is not H20", "H20 is not H200", "A10 is not A100", "no footnote after a digit"])
def test_model_numbers_match_exactly(statement, passage, absent):
    assert missing(statement, passage) == absent


def test_an_alias_is_not_matched_and_a_name_mentioned_elsewhere_still_counts():
    # No alias table: the Passage must use the asserted name (conservative).
    assert missing("AWS revenue rose 17%.", "Amazon Web Services revenue rose 17%.") == ["AWS"]
    # A multi-company Passage: the name is present, so who did what is left to entailment.
    assert missing("Tesla sold 2,256,714 BEVs, versus BYD's 1,636,129.",
                   "BYD sold 2,256,714 BEVs, beating Tesla's 1,636,129 deliveries.") == []


# ---- labels ----------------------------------------------------------------------------------

def test_a_descriptive_label_is_checked_only_through_the_names_the_statement_uses():
    passage = "Azure generated $75 billion in Microsoft’s fiscal year 2025, up 34%."
    statement = "Azure generated $75 billion in Microsoft's fiscal year 2025, up 34%."
    assert label_anchors("Microsoft Azure", statement) == {"Microsoft", "Azure"}
    assert missing(statement, passage, entities=["Microsoft Azure"]) == []
    write_down = "Nvidia said it would take a $5.5 billion write-down on inventory for its H20 chip."
    assert missing(write_down, write_down, entity="Nvidia H20 inventory write-down") == []


def test_a_label_naming_nothing_the_statement_says_imposes_nothing_and_is_reported():
    segment = "Cardiology devices contributed 341 million dollars of revenue in fiscal 2025."
    result = missing_names(statement=segment, entities=[], entity="Northwind Health",
                           passage_text=segment, common=CORPUS)
    assert result.missing == [] and result.labels_ignored == ["Northwind Health"]


def test_a_label_anchor_missing_from_the_passage_still_drops_it():
    row = "H20 related charges, net were 4,538 million for the three months."
    assert missing("NVIDIA's H20 charges, net, were 4,538 million.", row,
                   entity="NVIDIA Corporation") == ["NVIDIA"]


def test_an_entity_the_statement_itself_names_is_asserted_not_a_label():
    risk = "A single supplier provides the titanium used in orthopaedic implants."
    statement = "Northwind Health discloses a risk that a single supplier provides the titanium."
    assert missing(statement, risk, entities=["Northwind Health"]) == ["Northwind Health"]


def test_a_wrong_company_label_cannot_bypass_the_check_when_the_statement_names_it():
    assert missing("Tesla cut prices by 34%.", "BYD cut prices by 34%.",
                   entities=["Tesla"]) == ["Tesla"]
    assert missing("Prices were cut by 34%, Tesla said.", "BYD cut prices by 34%.") == ["Tesla"]
