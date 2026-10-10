import pytest
from pydantic import ValidationError

from insightforge_agent.benchmark.cases import (
    LABELS_BY_TYPE,
    POSITIVE_LABEL,
    BenchmarkCase,
    CaseFileError,
    CaseType,
    load_cases,
)

from .helpers import case_dict, make_case, reviewed, write_jsonl


def test_a_valid_case_of_every_type_loads():
    for case_type in ("PAIR", "SUPPORT", "GROUND", "INDEPENDENCE"):
        case = make_case(case_type, f"{case_type.lower()}-1")
        assert case.type == CaseType(case_type)
        assert case.label_status.value == "proposed"


def test_every_type_has_the_agreed_label_vocabulary():
    assert {t.value: {m.value for m in e} for t, e in LABELS_BY_TYPE.items()} == {
        "PAIR": {"SAME_FACT", "DIFFERENT_FACT", "CONTRADICTORY"},
        "SUPPORT": {"SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"},
        "GROUND": {"GROUNDED", "NOT_GROUNDED"},
        "INDEPENDENCE": {"INDEPENDENT", "DEPENDENT", "UNDETERMINED"},
    }
    assert {t.value: p.value for t, p in POSITIVE_LABEL.items()} == {
        "PAIR": "SAME_FACT", "SUPPORT": "SUPPORTED", "GROUND": "GROUNDED",
        "INDEPENDENCE": "INDEPENDENT",
    }


@pytest.mark.parametrize("missing", ["id", "type", "primary_category", "origin", "inputs",
                                     "expected", "rationale"])
def test_missing_required_fields_are_rejected(missing):
    record = case_dict()
    del record[missing]
    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate(record)


@pytest.mark.parametrize("overrides", [
    {"expected": "SUPPORTED"},                      # label of another type
    {"expected": "MAYBE"},                          # not a label at all
    {"primary_category": "  "},                     # empty category
    {"primary_category": ["a", "b"]},               # more than one category
    {"tags": ["x", "x"]},                           # duplicate tag
    {"tags": [""]},
    {"origin": "excerpt"},                          # excerpt without a source URL
    {"origin": "excerpt", "source_url": "ftp://x"},
    {"inputs": {"statement": "s", "passage": "p"}}, # SUPPORT inputs on a PAIR case
    {"inputs": {"statement_a": "a", "statement_b": ""}},
    {"inputs": {"statement_a": "a", "statement_b": "b", "extra": "x"}},
    {"rationale": ""},
    {"id": "has space"},
    {"id": "pair-same_fact-1"},                     # id leaks the answer
    {"grounding_failure": "UNSUPPORTED_ENTITY"},    # only valid on NOT_GROUNDED
    {"unknown_field": 1},
])
def test_invalid_records_are_rejected(overrides):
    with pytest.raises(ValidationError):
        make_case(**overrides)


def test_an_excerpt_case_with_a_source_url_is_valid():
    assert make_case(origin="excerpt", source_url="https://example.com/p").origin == "excerpt"


def test_grounding_failures_are_kept_for_not_grounded_cases():
    case = make_case("GROUND", expected="NOT_GROUNDED", grounding_failure="UNSUPPORTED_NUMBER")
    assert case.grounding_failure.value == "UNSUPPORTED_NUMBER"
    assert case.expected.value == "NOT_GROUNDED"


@pytest.mark.parametrize("overrides", [
    {"reviewer": "alice"},                                              # proposed with metadata
    {"label_status": "reviewed"},                                       # no reviewer/date
    {"label_status": "reviewed", "reviewer": "alice"},                  # no date
    {**reviewed(), "dispute_note": "unclear"},                          # reviewed with dispute
    {"label_status": "disputed"},                                       # no note
    {**reviewed(), "second_reviewer": "bob"},                           # partial second review
    {**reviewed(), "second_reviewer": "alice", "second_reviewed_at": "2026-10-02",
     "second_label": "SAME_FACT"},                                     # same person twice
    {**reviewed(), "second_reviewer": "bob", "second_reviewed_at": "2026-10-02",
     "second_label": "DIFFERENT_FACT"},                                    # disagreement yet reviewed
])
def test_inconsistent_review_metadata_is_rejected(overrides):
    with pytest.raises(ValidationError):
        make_case(**overrides)


def test_consistent_review_metadata_is_accepted():
    ok = make_case(**reviewed(second_reviewer="bob", second_reviewed_at="2026-10-02",
                              second_label="SAME_FACT"))
    assert ok.label_status.value == "reviewed"
    disputed = make_case(label_status="disputed", dispute_note="reviewers differ",
                         reviewer="alice", reviewed_at="2026-10-01")
    assert disputed.dispute_note == "reviewers differ"


def test_loading_reports_every_problem_with_its_line_number(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join([
            '{"id": "a1"',                                      # broken JSON (line 1)
            "[1, 2]",                                           # not an object (line 2)
            __import__("json").dumps(case_dict(case_id="ok1")),
            __import__("json").dumps(case_dict(case_id="ok1")),  # duplicate (line 4)
            __import__("json").dumps(case_dict(expected="SUPPORTED", case_id="bad1")),  # line 5
        ]),
        encoding="utf-8",
    )
    with pytest.raises(CaseFileError) as info:
        load_cases(path)
    problems = info.value.problems
    assert len(problems) == 4
    assert problems[0].startswith("line 1:")
    assert problems[1].startswith("line 2:")
    assert "duplicate id 'ok1'" in problems[2] and "first on line 3" in problems[2]
    assert problems[3].startswith("line 5:") and "expected" in problems[3]


def test_a_valid_file_loads_in_order_and_skips_blank_lines(tmp_path):
    path = write_jsonl(tmp_path / "c.jsonl", [case_dict(case_id="a"), case_dict("SUPPORT", "b")])
    path.write_text(path.read_text() + "\n\n", encoding="utf-8")
    assert [c.id for c in load_cases(path)] == ["a", "b"]


def test_the_committed_fixture_file_loads():
    from pathlib import Path

    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "benchmark" / "cases.jsonl"
    cases = load_cases(fixture)
    assert {c.type for c in cases} == set(CaseType)
    assert len({c.id for c in cases}) == len(cases)
