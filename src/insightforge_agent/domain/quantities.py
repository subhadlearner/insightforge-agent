"""Exact numeric quantities in text and in structured values (design.md sections 5 and 7).

Two representations of one number ("4,270,000" and "4.27 million") are equal only when their
exact decimal values are equal and, where a unit is stated, the units agree. Arithmetic is
`Decimal`, never float. Nothing here interprets qualifiers ("about", "~"), abbreviations
("4.27m"), rounding or different measures: those are left as different values."""

import re
from dataclasses import dataclass
from decimal import Decimal

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
MAGNITUDES = {"thousand": Decimal(10) ** 3, "million": Decimal(10) ** 6,
              "billion": Decimal(10) ** 9, "trillion": Decimal(10) ** 12}
_CURRENCY = {"$": "dollar", "€": "euro", "£": "pound", "usd": "dollar",
             "eur": "euro", "gbp": "pound"}
# Words that follow a number without naming what was counted.
_NOT_UNITS = frozenset(
    "a an and are as at be been but by compared during for from had has have in into is it its of "
    "on or over per since than that the their then these this those to up down was were which "
    "while with within versus vs after before about around nearly roughly approximately".split())

_QTY = re.compile(
    r"(?<![\w.,])(?P<cur>[$€£])?\s?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<mag>thousand|million|billion|trillion)\b)?"
    r"(?P<pct>\s*%)?(?:\s+(?P<word>[A-Za-z]+))?", re.IGNORECASE)
_VALUE = re.compile(
    r"^\s*(?P<cur>[$€£])?\s?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<mag>thousand|million|billion|trillion)\b)?"
    r"(?:\s*(?P<pct>%|percent\b|per\s*cent\b))?"
    r"(?:\s+(?P<units>[A-Za-z][A-Za-z ]*?))?\s*$", re.IGNORECASE)


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def _singular(word: str) -> str:
    word = word.casefold()
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


@dataclass(frozen=True)
class Quantity:
    value: Decimal   # exact, magnitude applied
    unit: str        # "" when none is stated
    surface: str     # the number and magnitude as written, normalised: "4.27 million", "4270000"
    number: str      # the number as `numbers_in` sees it


def _quantity(m: re.Match, unit: str) -> Quantity:
    number = m["num"].replace(",", "").rstrip(".")
    mag = (m["mag"] or "").casefold()
    return Quantity(Decimal(number) * MAGNITUDES.get(mag, 1), unit,
                    f"{number} {mag}".strip(), number)


def quantities_in(text: str) -> list[Quantity]:
    """Every number in the text with its magnitude word and, when one follows, its unit."""
    found = []
    for m in _QTY.finditer(text):
        word = (m["word"] or "").casefold()
        if m["cur"]:
            unit = _CURRENCY[m["cur"]]
        elif m["pct"] or word == "percent":
            unit = "percent"
        else:
            unit = "" if word in _NOT_UNITS or word in MAGNITUDES else _singular(word)
            unit = _CURRENCY.get(unit, unit)
        found.append(_quantity(m, unit))
    return found


def parse_value(value: str) -> Quantity | None:
    """A structured value that is exactly one quantity (number, optional magnitude, optional
    unit), or None. Qualifiers, ranges, several numbers and prose are not quantities."""
    m = _VALUE.match(value)
    if m is None:
        return None
    words = " ".join(_singular(w) for w in (m["units"] or "").split())
    if m["pct"] or words == "percent":
        unit = "percent"
    elif m["cur"]:
        unit = _CURRENCY[m["cur"]]
    else:
        unit = _CURRENCY.get(words, words)
    return _quantity(m, unit)


def canonical_value(value: str) -> str | None:
    """A key for exact equivalence ("4,270,000" and "4.27 million" give one key), or None for a
    value that is not a single exact quantity."""
    q = parse_value(value)
    return None if q is None else f"{format(q.value.normalize(), 'f')}|{q.unit}"


def unit_agrees(claimed: str, found: str) -> bool:
    """A unit the claim states must be the one the text states; a claim with no unit asks for
    none."""
    return not claimed or claimed == found


def unsupported_numbers(statement: str, text: str) -> set[str]:
    """Numbers of the statement that the text does not state. A number counts as stated when the
    text has the same figure with the same magnitude ("4.27 million" is not "4.27 billion"), or
    another representation of the same exact value with the claimed unit ("4.27 million
    vehicles" and "4,270,000 vehicles")."""
    in_text = quantities_in(text)
    missing = set()
    for q in quantities_in(statement):
        if any(p.value == q.value and (p.surface == q.surface or unit_agrees(q.unit, p.unit))
               for p in in_text):
            continue
        missing.add(q.number)
    return missing
