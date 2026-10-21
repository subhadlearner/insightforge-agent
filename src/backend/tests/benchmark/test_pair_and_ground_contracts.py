"""Regression tests for the PR #38 review corrections: PAIR structured fields, GROUND passage
fields, the second reviewer's typed label."""

import json

import pytest
from pydantic import ValidationError

from insightforge_agent.benchmark.cases import InputAccess, PredictionCase

from .helpers import case_dict, make_case, reviewed

MERGE = {"entity": "Acme", "predicate": "revenue", "value": "$5M", "scope": "group",
         "period": "2024", "entities": ["Acme"]}


def pair_with_structured():
    inputs = case_dict()["inputs"] | {"structured_a": MERGE,
                                      "structured_b": MERGE | {"entity": "Acme Inc"}}
    return make_case(inputs=inputs)


# ---- PAIR structured fields and input access --------------------------------------------------

def test_pair_cases_carry_the_structured_fields_the_merge_key_needs():
    case = pair_with_structured()
    assert case.inputs.structured_a.predicate == "revenue"
    assert case.inputs.structured_b.entity == "Acme Inc"


def test_structured_fields_come_in_pairs():
    with pytest.raises(ValidationError):
        make_case(inputs=case_dict()["inputs"] | {"structured_a": MERGE})


def test_semantic_candidates_get_statements_and_passages_only():
    case = pair_with_structured()
    semantic = case.prediction_view()  # the default is the safe one
    assert semantic.access is InputAccess.SEMANTIC
    dumped = json.dumps(semantic.model_dump(mode="json"))
    assert "Acme Inc" not in dumped and '"predicate"' not in dumped and '"entity"' not in dumped
    assert semantic.inputs.structured_a is None and semantic.inputs.statement_a
    baseline = case.prediction_view(InputAccess.BASELINE)
    assert baseline.inputs.structured_b.entity == "Acme Inc"
    assert baseline.access is InputAccess.BASELINE


def test_a_semantic_view_cannot_be_built_with_the_baseline_fields():
    case = pair_with_structured()
    with pytest.raises(ValidationError):
        PredictionCase(id=case.id, type=case.type, inputs=case.inputs)  # SEMANTIC by default


def test_the_two_views_hash_differently_so_recordings_cannot_cross():
    from insightforge_agent.benchmark.recording import case_hash

    case = pair_with_structured()
    assert case_hash(case.prediction_view()) != case_hash(case.prediction_view(InputAccess.BASELINE))


# ---- GROUND passage, span and attribution -----------------------------------------------------

GROUND_BASE = {"claim": "Acme revenue grew 10%.", "passage_text": "Acme. Revenue grew 10% in 2024.",
               "passage_ref": {"observation_id": "obs-9", "index": 3}}


def ground(**inputs):
    return make_case("GROUND", inputs=GROUND_BASE | inputs)


def test_a_quoted_span_case_keeps_passage_identity_span_and_attribution_context():
    case = ground(check="QUOTED_SPAN", quoted_span="Revenue grew 10%",
                  attribution_context="Heading: Acme annual results")
    i = case.inputs
    assert (i.passage_ref.observation_id, i.passage_ref.index) == ("obs-9", 3)
    assert i.quoted_span == "Revenue grew 10%" and i.attribution_context.startswith("Heading")
    assert case.prediction_view().inputs.passage_text.startswith("Acme.")


def test_t6_claim_versus_passage_is_a_separate_check_without_a_span():
    case = ground(check="CLAIM_PASSAGE", historical=True)
    assert case.inputs.check.value == "CLAIM_PASSAGE" and case.inputs.historical is True


@pytest.mark.parametrize("inputs", [
    {"check": "QUOTED_SPAN"},                                   # span missing
    {"check": "QUOTED_SPAN", "quoted_span": "  "},
    {"check": "CLAIM_PASSAGE", "quoted_span": "Revenue grew"},  # T6 quotes no span
    {"check": "QUOTED_SPAN", "quoted_span": "x", "passage_ref": {"observation_id": "o", "index": -1}},
    {"check": "QUOTED_SPAN", "quoted_span": "x", "passage_text": ""},
    {"check": "SOMETHING_ELSE"},
])
def test_invalid_ground_inputs_are_rejected(inputs):
    with pytest.raises(ValidationError):
        ground(**inputs)


def test_a_nonexistent_span_is_representable_as_a_not_grounded_case():
    case = make_case("GROUND", expected="NOT_GROUNDED", grounding_failure="UNSUPPORTED_CLAIM",
                     inputs=GROUND_BASE | {"check": "QUOTED_SPAN", "quoted_span": "never written"})
    assert case.expected.value == "NOT_GROUNDED"


# ---- second reviewer's typed judgment ---------------------------------------------------------

SECOND = {"second_reviewer": "bob", "second_reviewed_at": "2026-10-09"}


def test_the_second_reviewers_actual_label_is_preserved():
    case = make_case(**reviewed(**SECOND, second_label="SAME_FACT"))
    assert case.second_label.value == "SAME_FACT" and case.second_review_agrees is True


def test_a_differing_second_label_requires_a_dispute_and_both_labels_survive():
    with pytest.raises(ValidationError):
        make_case(**reviewed(**SECOND, second_label="CONTRADICTORY"))
    disputed = make_case(label_status="disputed", dispute_note="needs adjudication",
                         reviewer="alice", reviewed_at="2026-10-01", **SECOND,
                         second_label="CONTRADICTORY")
    assert disputed.expected.value == "SAME_FACT"  # the first label is untouched
    assert disputed.second_label.value == "CONTRADICTORY"
    assert disputed.second_review_agrees is False
    reloaded = type(disputed).model_validate_json(disputed.model_dump_json())
    assert reloaded.second_label.value == "CONTRADICTORY"


def test_a_second_label_must_belong_to_the_case_type_and_the_boolean_is_gone():
    with pytest.raises(ValidationError):
        make_case(**reviewed(**SECOND, second_label="SUPPORTED"))
    with pytest.raises(ValidationError):
        make_case(**reviewed(**SECOND, second_review_agrees=True))
    with pytest.raises(ValidationError):  # date and reviewer without a label
        make_case(**reviewed(**SECOND))
