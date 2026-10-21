"""ADR-0006: the deterministic half of the evidence-bound support contract, on the adjudicated
gold set (tests/fixtures/research_integrity/support_cases.json).

The gold labels are independent of production code: they were written from the cited text
alone. Three permissive-judge personas show the *deterministic floor*: what code refuses even
when a model wrongly answers SUPPORTED.

- full: the whole statement as one part, the whole Passage as its excerpt.
- lazy: like full, but leaving the case's unstated qualifier out of the parts.
- identity: the case's subject taken from identity context, the rest from the whole Passage.
- sentence: like full, but quoting only the Passage's best-matching sentence, so an operator
  elsewhere in the Passage (an unrelated "does not") cannot pass for a floor.

Offline only: these tests say nothing about how often a real model answers correctly."""

import json
from pathlib import Path

import pytest

from insightforge_agent.domain.support import (
    SupportPart,
    check_support,
    content_terms,
    find_excerpt,
    identity_problem,
    uncovered_terms,
)
from insightforge_agent.domain.passages import sentences_of
from insightforge_agent.pipeline.synthesize import literal_problem

CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "research_integrity"
                    / "support_cases.json").read_text(encoding="utf-8"))["cases"]
POSITIVE = [c for c in CASES if c["expected"] == "SUPPORTED"]
NEGATIVE = [c for c in CASES if c["class"] == "negative"]


def parts(spec: list[dict]) -> list[SupportPart]:
    return [SupportPart(p["part"], tuple(p.get("excerpts", [])), p.get("identity", False))
            for p in spec]


def persona(case: dict, name: str) -> list[SupportPart]:
    statement, passage = case["statement"].rstrip("."), case["passage"]
    if name == "full":
        return [SupportPart(statement, (passage,))]
    if name == "sentence":
        terms = set(content_terms(statement))
        best = max(sentences_of(passage), key=lambda s: len(terms & set(content_terms(s))))
        return [SupportPart(statement, (best,))]
    if name == "lazy":
        return [SupportPart(statement.replace(case["qualifier"], " "), (passage,))]
    subject = case["subject"]
    return [SupportPart(subject, identity=True),
            SupportPart(statement.replace(subject, " "), (passage,))]


@pytest.mark.parametrize("case", POSITIVE, ids=[c["id"] for c in POSITIVE])
def test_every_gold_positive_is_supported_with_its_honest_map(case):
    check = check_support(case["statement"], parts(case["honest_map"]), case["passage"],
                          case["document_entity"])
    assert check.verdict == "SUPPORTED", check.problems


@pytest.mark.parametrize("case", NEGATIVE, ids=[c["id"] for c in NEGATIVE])
def test_the_deterministic_floor_of_every_gold_negative_is_as_recorded(case):
    measured = {}
    for name in case["floor"]:
        check = check_support(case["statement"], persona(case, name), case["passage"],
                              case["document_entity"])
        measured[name] = "model" if check.verdict == "SUPPORTED" else "code"
    assert measured == case["floor"]


def test_the_floor_blocks_every_figure_period_and_identity_negative():
    """The classes code can and must stop alone, whatever the model says."""
    blocked = {c["id"] for c in NEGATIVE
               if any(v == "code" for k, v in c["floor"].items() if k in ("full", "identity"))}
    assert {"NW-N01", "NW-N02", "NW-N03", "WEB-N01", "WEB-N02"} <= blocked


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_retired_literal_gate_is_recorded_faithfully(case):
    """The T18 lexical gate on each case, as recorded: it refused the Stage C positive NW-P01
    before any judge saw it, and passed every qualifier-leak negative."""
    assert literal_problem(statement=case["statement"], entities=case["entities"],
                           entity=case["entity"], passage_text=case["passage"],
                           document_entity=case["document_entity"]) == case["old_literal_gate"]


def test_a_fabricated_excerpt_is_a_verification_error_never_support():
    case = next(c for c in CASES if c["id"] == "Q-N01")
    check = check_support(case["statement"], [SupportPart(case["statement"],
                                                          ("Microsoft held 20% of cloud infrastructure services",))],
                          case["passage"], None)
    assert check.verdict == "VERIFICATION_ERROR"


def test_a_supported_verdict_without_a_map_is_not_support():
    assert check_support("BYD sold 4.27 million vehicles.", [], "BYD sold 4.27 million vehicles.",
                         None).verdict == "INSUFFICIENT_EVIDENCE"


def test_excerpts_match_whole_words_and_ignore_case_punctuation_and_quotes():
    text = "Alphabet’s planned $175-185 billion is notable."
    assert find_excerpt("alphabet's PLANNED $175-185 billion", text) == (0, 35)
    assert find_excerpt("Alphabet planned", text) is None  # possessive "s" is a word here
    assert find_excerpt("plan", text) is None  # never part of a word
    assert find_excerpt("1,234 units", "It sold 1234 units.") is not None


def test_coverage_counts_content_words_and_their_plurals_only():
    assert content_terms("The pipeline holds 6 programmes in 2025.") == [
        "pipeline", "hold", "6", "programme", "2025"]
    assert uncovered_terms("Microsoft held 20% of cloud infrastructure services",
                           ["Microsoft held 20%"]) == ["cloud", "infrastructure", "service"]


@pytest.mark.parametrize("part, entity, ok", [
    ("Northwind Health", "Northwind Health", True),
    ("Northwind Health's", "Northwind Health", True),
    ("Northwind", "Northwind Health", True),
    ("Medtronic", "Northwind Health", False),
    ("Northwind Health pipeline", "Northwind Health", False),  # a label is not the company
    ("Northwind 2025", "Northwind Health", False),
    ("Northwind Health", None, False),
])
def test_identity_is_only_the_documents_own_entity(part, entity, ok):
    assert (identity_problem(part, entity) is None) is ok
