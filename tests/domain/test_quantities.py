from decimal import Decimal

import pytest

from insightforge_agent.domain.contracts import ExtractedFact, PassageRef
from insightforge_agent.domain.extraction import detect_conflicts, same_value
from insightforge_agent.domain.passages import value_in_text
from insightforge_agent.domain.quantities import (
    canonical_value,
    parse_value,
    quantities_in,
    unsupported_numbers,
)


@pytest.mark.parametrize("a,b", [
    ("4.27 million", "4,270,000"),
    ("4.27 million", "4270000"),
    ("4.27 million", "4,270,000.00"),
    ("4.27 million vehicles", "4,270,000 vehicles"),
    ("4.270 million", "4.27 million"),
    ("1.5 billion", "1,500 million"),
    ("0.3 million", "300,000"),
    ("$32,000", "32,000 dollars"),
    ("20%", "20 percent"),
])
def test_exact_representations_of_one_quantity_are_the_same_value(a, b):
    assert canonical_value(a) == canonical_value(b) is not None
    assert same_value(a, b)


@pytest.mark.parametrize("a,b", [
    ("4.27 million", "4.27 billion"),                 # different magnitude
    ("4.27 million dollars", "4.27 million vehicles"),  # different unit
    ("4.27 million", "4.27 million vehicles"),        # a stated unit versus none
    ("4.27 million", "about 4.27 million"),           # a qualifier is not an exact value
    ("4.27 million", "~4.27 million"),
    ("4.3 million", "4,270,000"),                     # rounded versus exact
    ("4.27 million", "4.27m"),                        # abbreviations are not read
    ("20%", "20 vehicles"),
    ("32,000 dollars", "32,000 euros"),
    ("4.27 million", "4.28 million"),
])
def test_different_magnitudes_units_qualifiers_and_roundings_are_not_the_same_value(a, b):
    assert not same_value(a, b)


def test_arithmetic_is_exact_not_floating_point():
    assert parse_value("0.1 million").value == Decimal(100000)
    assert parse_value("0.3 million").value == Decimal(300000)
    assert canonical_value("0.30000000000000004 million") != canonical_value("0.3 million")


@pytest.mark.parametrize("value", ["about 4.27 million", "4.27 million, down 8.6%", "4 to 5 million",
                                   "Zhang Yong", "", "2.17M (Tesla 1.61M)"])
def test_values_that_are_not_one_exact_quantity_have_no_canonical_form(value):
    assert canonical_value(value) is None


def test_non_quantity_values_still_compare_as_written():
    assert same_value("Zhang Yong", " zhang  yong ")
    assert not same_value("Zhang Yong", "Zhang Wei")
    assert same_value("about 4.27 million", "About  4.27 million")


@pytest.mark.parametrize("statement,text,missing", [
    ("BYD sold 4.27 million vehicles in 2025.", "BYD sold 4,270,000 vehicles in 2025.", set()),
    ("BYD sold 4,270,000 vehicles in 2025.", "BYD sold 4.27 million vehicles in 2025.", set()),
    ("BYD sold 4.27 million vehicles in 2025.", "BYD sold 4.27 million vehicles in 2025.", set()),
    ("BYD sold 4.27 million vehicles in 2025.", "BYD sold 4.27 billion vehicles in 2025.", {"4.27"}),
    ("BYD sold 4.27 million vehicles in 2025.", "BYD sold 4,270,000 dollars in 2025.", {"4.27"}),
    ("BYD sold 4.27 million dollars in 2025.", "BYD sold 4,270,000 vehicles in 2025.", {"4.27"}),
    ("BYD sold 4.27 million vehicles in 2025.", "BYD sold 4.3 million vehicles in 2025.", {"4.27"}),
    ("Sales rose 27% in 2025.", "Sales rose 27 percent in 2025.", set()),
    # the same figure written the same way keeps the earlier leniency: the entailment call, not
    # this check, judges what it measures
    ("Sales rose 27% in 2025.", "Sales rose 27 vehicles in 2025.", set()),
    ("It cost 32,000 dollars.", "It cost $32,000 here.", set()),
    ("BYD sold 9.99 million vehicles.", "BYD sold 4.27 million vehicles.", {"9.99"}),
])
def test_a_statements_numbers_must_be_stated_with_matching_magnitude_and_units(
        statement, text, missing):
    assert unsupported_numbers(statement, text) == missing


def test_quantity_extraction_reads_magnitudes_and_units():
    q = quantities_in("Sold 4.27 million vehicles in 2025, up 27% to $32,000.")
    assert [(x.value, x.unit) for x in q] == [
        (Decimal(4270000), "vehicle"), (Decimal(2025), ""), (Decimal(27), "percent"),
        (Decimal(32000), "dollar")]


@pytest.mark.parametrize("value,text,expected", [
    ("4.27 million", "BYD sold 4,270,000 vehicles", True),
    ("4,270,000", "BYD sold 4.27 million vehicles", True),
    ("4.27 million vehicles", "BYD sold 4,270,000 vehicles", True),
    ("4.27 million dollars", "BYD sold 4,270,000 vehicles", False),
    ("4.27 million", "BYD sold 4.27 billion vehicles", False),
    ("4.3 million", "BYD sold 4,270,000 vehicles", False),
])
def test_a_structured_value_may_be_stated_in_another_exact_representation(value, text, expected):
    assert value_in_text(value, text) is expected


def fact(fid, value, period="2025"):
    return ExtractedFact(id=fid, source_id="s", passage=PassageRef(observation_id="o", index=0),
                         statement=f"BYD {value}", entity="BYD", predicate="units_sold",
                         value=value, period=period)


def test_equivalent_number_formats_are_not_conflicts_but_different_values_still_are():
    _, none = detect_conflicts([fact("f1", "4.27 million"), fact("f2", "4,270,000")])
    assert none == []
    _, conflicts = detect_conflicts([fact("f1", "4.27 million"), fact("f2", "4.27 billion")])
    assert [c.fact_ids for c in conflicts] == [["f1", "f2"]]
    _, conflicts = detect_conflicts([fact("f1", "4.27 million"), fact("f2", "4.01 million")])
    assert len(conflicts) == 1
