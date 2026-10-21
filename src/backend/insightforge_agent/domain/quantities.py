"""Exact numeric quantities in text and in structured values (design.md sections 5 and 7).

One job: decide whether two *written numbers* are the same quantity. Two representations are the
same only when their exact `Decimal` values are equal and any unit the claim states agrees.

    "4,270,000"  == "4.27 million"          "$32,000" == "32,000 dollars"      "20%" == "20 percent"
    "4.27 million" != "4.27 billion"        "4.27 million dollars" != "4.27 million vehicles"
    "4.27 million" != "about 4.27 million"  "4.3 million" != "4,270,000"        "4.27m" is not read

The module is layered, each layer using only the one above it:

    1. parsing        `quantities_in` (numbers inside prose), `parse_value` (a whole structured value)
    2. normalisation  `canonical_value` (a comparable key for a structured value)
    3. comparison     `stated_by` (does a found quantity state a claimed one?), `unit_agrees`
    4. validation     `unsupported_numbers` (which numbers of a statement a Passage does not state)

Deliberately NOT here: qualifiers, rounding, abbreviations, ranges, and anything about *what* is
measured. Numeric normalisation is one form of semantic equivalence among several. The others are
entity resolution ("BYD" / "BYD Auto"), predicate normalisation (`units_sold` / `sales_volume`),
value normalisation beyond exact quantities ("about 4.27 million"), and temporal normalisation
("2025" / "2025-12"). They need a controlled vocabulary and are deferred to T12; this module is not
meant to grow into a generic semantic engine."""

import re
from dataclasses import dataclass
from decimal import Decimal

# --- vocabulary -------------------------------------------------------------------------------

MAGNITUDES = {"thousand": Decimal(10) ** 3, "million": Decimal(10) ** 6,
              "billion": Decimal(10) ** 9, "trillion": Decimal(10) ** 12}

_CURRENCY = {"$": "dollar", "€": "euro", "£": "pound",  # signs
             "usd": "dollar", "eur": "euro", "gbp": "pound"}      # codes

# Words that can follow a number without naming what was counted ("4.27 million in 2025").
_NOT_UNITS = frozenset(
    "a an and are as at be been but by compared during for from had has have in into is it its of "
    "on or over per since than that the their then these this those to up down was were which "
    "while with within versus vs after before about around nearly roughly approximately".split())

# --- patterns, composed from named pieces ------------------------------------------------------

_NUMBER = r"\d[\d,]*(?:\.\d+)?"                  # 4  4.27  4,270,000
_MAGNITUDE = "|".join(MAGNITUDES)                # thousand|million|...
_SIGN = r"[$€£]"                       # $ EUR GBP

# A quantity inside prose: [sign] number [magnitude] [%] [following word]
_IN_PROSE = re.compile(
    rf"(?<![\w.,])(?P<sign>{_SIGN})?\s?(?P<number>{_NUMBER})"
    rf"(?:\s*(?P<magnitude>{_MAGNITUDE})\b)?(?P<percent>\s*%)?(?:\s+(?P<word>[A-Za-z]+))?",
    re.IGNORECASE)

# A whole structured value: the same pieces, but nothing else may be present.
_WHOLE_VALUE = re.compile(
    rf"^\s*(?P<sign>{_SIGN})?\s?(?P<number>{_NUMBER})"
    rf"(?:\s*(?P<magnitude>{_MAGNITUDE})\b)?(?:\s*(?P<percent>%|percent\b|per\s*cent\b))?"
    rf"(?:\s+(?P<units>[A-Za-z][A-Za-z ]*?))?\s*$",
    re.IGNORECASE)


def numbers_in(text: str) -> set[str]:
    """Every figure in the text without separators: "1,764,992 in 2024" -> {"1764992", "2024"}."""
    return {n.replace(",", "").rstrip(".") for n in re.findall(_NUMBER, text)}


# --- 1. parsing --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Quantity:
    """One written number: its exact value (magnitude applied), unit and how it was written."""

    value: Decimal   # exact: "4.27 million" -> Decimal(4270000)
    unit: str        # "vehicle", "dollar", "percent", or "" when none is stated
    surface: str     # figure and magnitude as written, normalised: "4.27 million"
    number: str      # the figure alone, as `numbers_in` sees it: "4.27"


def _singular(word: str) -> str:
    word = word.casefold()
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _unit(*, sign: str | None, percent: bool, words: str) -> str:
    """Percent beats a currency sign beats a unit word; currency codes and names are folded
    ("dollars", "usd", "$" -> "dollar")."""
    if percent:
        return "percent"
    if sign:
        return _CURRENCY[sign]
    unit = " ".join(_singular(w) for w in words.split())
    return _CURRENCY.get(unit, unit)


def _quantity(match: re.Match, unit: str) -> Quantity:
    number = match["number"].replace(",", "").rstrip(".")
    magnitude = (match["magnitude"] or "").casefold()
    return Quantity(Decimal(number) * MAGNITUDES.get(magnitude, 1), unit,
                    f"{number} {magnitude}".strip(), number)


def quantities_in(text: str) -> list[Quantity]:
    """Every number in prose, with its magnitude and, when one follows, its unit.

    "Sold 4.27 million vehicles in 2025, up 27%" ->
        (4270000, "vehicle"), (2025, ""), (27, "percent")"""
    found = []
    for m in _IN_PROSE.finditer(text):
        word = (m["word"] or "").casefold()
        is_unit = word not in _NOT_UNITS and word not in MAGNITUDES
        found.append(_quantity(m, _unit(
            sign=m["sign"], percent=bool(m["percent"]) or word == "percent",
            words=word if is_unit else "")))
    return found


def parse_value(value: str) -> Quantity | None:
    """A structured value that is exactly one quantity, or None.

    "4.27 million" and "$32,000" and "20 percent" parse; "about 4.27 million", "4 to 5 million",
    "4.27 million, down 8.6%" and "Zhang Yong" do not (qualifiers, ranges, several numbers and
    prose are not quantities)."""
    m = _WHOLE_VALUE.match(value)
    if m is None:
        return None
    words = m["units"] or ""
    return _quantity(m, _unit(sign=m["sign"], percent=bool(m["percent"]) or words.casefold() == "percent",
                              words=words))


# --- 2. normalisation --------------------------------------------------------------------------

def canonical_value(value: str) -> str | None:
    """A key under which equivalent structured values collide, or None for a value that is not a
    single exact quantity.

    "4.27 million" and "4,270,000" -> "4270000|"; "4.27 million vehicles" -> "4270000|vehicle"."""
    q = parse_value(value)
    return None if q is None else f"{format(q.value.normalize(), 'f')}|{q.unit}"


# --- 3. comparison -----------------------------------------------------------------------------

def unit_agrees(claimed: str, found: str) -> bool:
    """A unit the claim states must be the one found; a claim with no unit asks for none."""
    return not claimed or claimed == found


def stated_by(claimed: Quantity, found: Quantity) -> bool:
    """Whether `found` states the claimed quantity: the same exact value, and a matching unit.

    "4.27 million vehicles" is stated by "4,270,000 vehicles" but not by "4,270,000 dollars"."""
    return claimed.value == found.value and unit_agrees(claimed.unit, found.unit)


# --- 4. validation -----------------------------------------------------------------------------

def unsupported_numbers(statement: str, text: str) -> set[str]:
    """Figures of the statement that the text does not state, as `numbers_in` strings.

    A figure is stated when the text has it written the same way with the same magnitude
    ("4.27 million" is not "4.27 billion"; the unit is left to the entailment call), or the same
    exact value written differently with the claimed unit ("4.27 million vehicles" and
    "4,270,000 vehicles")."""
    in_text = quantities_in(text)
    missing = set()
    for claimed in quantities_in(statement):
        if not any(p.value == claimed.value and (p.surface == claimed.surface or stated_by(claimed, p))
                   for p in in_text):
            missing.add(claimed.number)
    return missing
