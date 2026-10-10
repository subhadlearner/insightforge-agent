"""The 48-case pilot set (benchmarks/evidence/cases.jsonl, issue #32) and the few-shot set.

These tests pin the agreed composition, so the dataset cannot drift without a visible change."""

import re
from collections import Counter
from pathlib import Path

import pytest

from insightforge_agent.benchmark.cases import (
    POSITIVE_LABEL,
    BenchmarkCase,
    CaseType,
    LabelStatus,
    load_cases,
)

ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "evidence"
CASES = ROOT / "cases.jsonl"
FEWSHOT = ROOT / "fewshot.jsonl"

COMPOSITION = {
    CaseType.PAIR: {
        "false_merge_negative": 6, "period_scope_mismatch": 4, "alias_predicate_synonym": 4,
        "paraphrase_qualified_value": 3, "contradiction": 4, "temporal_ambiguity": 3,
    },
    CaseType.SUPPORT: {
        "irrelevant_quotation": 3, "forecast_vs_historical": 3, "t6_similarity_numeric": 4,
        "positive_control": 2,
    },
    CaseType.GROUND: {
        "wrong_interpretation": 2, "misattribution": 2, "qualifiers_units_abbreviations": 3,
        "nonexistent_span": 1,
    },
    CaseType.INDEPENDENCE: {"syndicated": 2, "genuinely_independent": 2},
}
# Critical: every PAIR negative (false-merge risk, including period/scope and temporal negatives),
# every SUPPORT negative, and the syndicated INDEPENDENCE cases.
CRITICAL_SUPPORT_AND_INDEPENDENCE = {
    "irrelevant_quotation", "forecast_vs_historical", "t6_similarity_numeric", "syndicated",
}
LOOK_ALIKES = {
    "million_vs_billion", "dollars_vs_vehicles", "rounded_vs_exact", "same_name_different_entity",
    "same_number_different_period", "same_number_different_scope", "syndicated_copy",
}


@pytest.fixture(scope="module")
def cases() -> list[BenchmarkCase]:
    return load_cases(CASES)


@pytest.fixture(scope="module")
def fewshot() -> list[BenchmarkCase]:
    return load_cases(FEWSHOT)


def test_the_set_has_48_cases_in_the_agreed_composition(cases):
    assert len(cases) == 48
    by_type = Counter(c.type for c in cases)
    assert by_type == {CaseType.PAIR: 24, CaseType.SUPPORT: 12, CaseType.GROUND: 8,
                       CaseType.INDEPENDENCE: 4}
    for case_type, categories in COMPOSITION.items():
        found = Counter(c.primary_category for c in cases if c.type is case_type)
        assert dict(found) == categories, case_type


def test_every_label_starts_proposed_with_no_review_data(cases):
    assert {c.label_status for c in cases} == {LabelStatus.PROPOSED}
    assert all(c.reviewer is None and c.second_label is None for c in cases)


def test_the_set_is_negative_heavy_with_both_controls_for_every_type(cases):
    for case_type in CaseType:
        of_type = [c for c in cases if c.type is case_type]
        positives = [c for c in of_type if c.expected == POSITIVE_LABEL[case_type]]
        assert positives, f"{case_type} has no positive control"
        assert len(positives) < len(of_type), f"{case_type} has no negative control"
    negatives = [c for c in cases if c.expected != POSITIVE_LABEL[c.type]]
    assert len(negatives) > len(cases) / 2


def test_critical_is_set_exactly_for_the_costly_categories(cases):
    for case in cases:
        expected = (case.type is CaseType.PAIR and case.expected != POSITIVE_LABEL[case.type])             or case.primary_category in CRITICAL_SUPPORT_AND_INDEPENDENCE
        assert case.critical == expected, case.id
    assert {c.id for c in cases if c.critical and c.primary_category in
            {"period_scope_mismatch", "temporal_ambiguity"}} == {
        "p-ps-01", "p-ps-02", "p-ps-03", "p-ps-04", "p-ta-02", "p-ta-03"}
    assert sum(c.critical for c in cases) == 28


def test_the_contradictory_single_valued_pair_states_one_exact_date(cases):
    case = next(c for c in cases if c.id == "p-ct-03")
    assert case.inputs.statement_a.endswith("as of 1 March 2025.")
    assert case.inputs.statement_b.endswith("as of 1 March 2025.")


def test_critical_cases_are_never_positives_except_by_construction(cases):
    # A critical case is one whose wrong positive prediction is costly, so it is a negative.
    for case in (c for c in cases if c.critical):
        assert case.expected != POSITIVE_LABEL[case.type], case.id


def test_every_agreed_look_alike_is_present(cases):
    tags = {t for c in cases for t in c.tags}
    assert LOOK_ALIKES <= tags


def test_origin_is_synthetic_or_an_excerpt_with_a_url(cases):
    for case in cases:
        assert case.origin in {"synthetic", "excerpt"}
        if case.origin == "excerpt":
            assert (case.source_url or "").startswith("http")
        assert case.rationale.strip()


def test_not_grounded_cases_say_why_and_others_do_not(cases):
    for case in (c for c in cases if c.type is CaseType.GROUND):
        assert (case.grounding_failure is not None) == (case.expected.value == "NOT_GROUNDED")


def test_pair_cases_carry_structured_fields_for_the_baseline(cases):
    for case in (c for c in cases if c.type is CaseType.PAIR):
        assert case.inputs.structured_a is not None and case.inputs.structured_b is not None, case.id


def test_ids_are_unique_and_do_not_leak_the_label(cases):
    assert len({c.id for c in cases}) == len(cases)  # load_cases also rejects duplicates


# ---- the few-shot set -------------------------------------------------------------------------

def _fingerprint(case: BenchmarkCase) -> frozenset[str]:
    """The normalised text of every string input: two cases with the same text are the same case."""
    texts: set[str] = set()
    for name, value in case.inputs:
        if isinstance(value, str) and name != "check":  # `check` is a vocabulary word, not text
            texts.add(re.sub(r"\W+", " ", value).strip().casefold())
    return frozenset(t for t in texts if t)


def test_the_few_shot_set_is_small_and_valid(fewshot):
    assert 0 < len(fewshot) <= 10
    assert {c.label_status for c in fewshot} == {LabelStatus.PROPOSED}


def test_few_shot_examples_do_not_overlap_benchmark_cases(cases, fewshot):
    assert {c.id for c in fewshot}.isdisjoint({c.id for c in cases})
    benchmark_texts = {t for c in cases for t in _fingerprint(c)}
    for example in fewshot:
        shared = _fingerprint(example) & benchmark_texts
        assert not shared, f"{example.id} reuses benchmark text: {sorted(shared)[:2]}"


def test_few_shot_examples_do_not_share_entities_with_the_benchmark(cases, fewshot):
    """Stricter than text equality: a near-copy with a changed number would still leak."""
    def names(case: BenchmarkCase) -> set[str]:
        text = " ".join(v for _, v in case.inputs if isinstance(v, str))
        return set(re.findall(r"\b[A-Z][a-z]+ (?:[A-Z][a-z]+|Air|Bio|Foods|Motors|Energy)\b", text))
    benchmark_names = {n for c in cases for n in names(c)}
    for example in fewshot:
        assert names(example).isdisjoint(benchmark_names), example.id
