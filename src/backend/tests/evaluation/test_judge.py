"""Synthesis quality (T16 #17): one LLM-judge call per Report against a fixed, versioned rubric.
A reply that is not a complete, in-range score sheet is an error, never a score; the judge
scores synthesis only and sees no Passage text."""

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from insightforge_agent.evaluation.judge import (
    CRITERIA,
    RUBRIC_VERSION,
    judge_synthesis,
    not_judged,
)
from insightforge_agent.evaluation.library import load_library
from tests.evaluation.builders import World, claim, historical, item, report, section, verdict
from tests.scripted import ScriptedChat
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT
SAMPLE = next(s for s in load_library(ROOT / "samples" / "briefs", ROOT)
              if s.id == "memory-prior-report")
PASSAGE_TEXT = "PASSAGE-ONLY-TEXT BYD sold 4.27 million vehicles in 2025."


def judge(reply: str) -> ScriptedChat:
    return ScriptedChat(script=[AIMessage(content=reply)])


def sheet(**scores) -> str:
    full = {c: 4 for c in CRITERIA} | scores
    return json.dumps({**full, "rationale": "Clear and well organised."})


def stored():
    w = World()
    [p] = w.web("https://a.example/1", PASSAGE_TEXT)
    [old] = w.web("https://old.example/1", "BYD led LFP batteries.")
    r = report([section("Recent sales and pricing", claim("BYD sold 4.27 million vehicles.",
                                                          "ev_1")),
                section("Earlier findings", claim("BYD led LFP batteries (reported 2026-03-01).",
                                                  "hist_1", historical_=True))],
               [item("ev_1", p)], [historical("hist_1", old)],
               verdicts=[verdict(0, 0, "ev_1", "unverified", p)], gaps=["Pricing in Europe."])
    return r


def test_a_complete_score_sheet_is_judged_with_its_rubric_version():
    result = judge_synthesis(judge(sheet(coverage=5, synthesis=3)), SAMPLE, stored())
    assert result.status == "judged" and result.rubric_version == RUBRIC_VERSION
    assert result.scores == {"coverage": 5, "organisation": 4, "synthesis": 3, "calibration": 4}
    assert result.score == 4.0


def test_the_judge_sees_the_brief_the_expected_structure_and_the_report_as_published():
    model = judge(sheet())
    judge_synthesis(model, SAMPLE, stored())
    [conversation] = model.seen
    system = next(str(m.content) for m in conversation if isinstance(m, SystemMessage))
    user = next(str(m.content) for m in conversation if isinstance(m, HumanMessage))
    assert SAMPLE.brief in user
    for expected in SAMPLE.expected_report_structure.sections:
        assert expected.heading in user
    assert "BYD sold 4.27 million vehicles. [UNVERIFIED]" in user  # the published draft
    assert "(historical) BYD led LFP batteries" in user
    assert "Pricing in Europe." in user
    assert "PASSAGE-ONLY-TEXT" not in user  # synthesis is judged without Passage text
    assert "Do not judge factual accuracy" in system and RUBRIC_VERSION in system


def test_a_reply_that_is_not_json_is_an_error_not_a_score():
    result = judge_synthesis(judge("The report is good. 4/5."), SAMPLE, stored())
    assert result.status == "error" and result.score is None and result.scores is None
    assert result.reason


def test_a_missing_criterion_is_an_error():
    partial = json.loads(sheet())
    del partial["calibration"]
    result = judge_synthesis(judge(json.dumps(partial)), SAMPLE, stored())
    assert result.status == "error" and result.score is None


def test_an_out_of_range_or_fractional_score_is_an_error():
    for bad in (6, 0, 3.5):
        result = judge_synthesis(judge(sheet(coverage=bad)), SAMPLE, stored())
        assert result.status == "error" and result.score is None, bad


def test_an_extra_criterion_is_an_error():
    extra = json.loads(sheet()) | {"accuracy": 5}
    assert judge_synthesis(judge(json.dumps(extra)), SAMPLE, stored()).status == "error"


ABSENT = object()


def with_rationale(value) -> str:
    full = json.loads(sheet())
    if value is ABSENT:
        del full["rationale"]
    else:
        full["rationale"] = value
    return json.dumps(full)


def test_a_rationale_is_trimmed_and_kept_up_to_600_characters():
    result = judge_synthesis(judge(with_rationale("  Clear and well organised.\n")), SAMPLE,
                             stored())
    assert result.status == "judged" and result.rationale == "Clear and well organised."
    longest = "x" * 600
    padded = judge_synthesis(judge(with_rationale(f"\t{longest}  ")), SAMPLE, stored())
    assert padded.status == "judged" and padded.rationale == longest and padded.score == 4.0


@pytest.mark.parametrize("value", [ABSENT, None, "", "   \n\t", 42, ["Clear."],
                                   {"text": "Clear."}, "x" * 601, " " + "x" * 601 + " "],
                         ids=["absent", "null", "empty", "whitespace", "number", "list",
                              "object", "601", "601-padded"])
def test_a_score_sheet_without_a_usable_rationale_is_an_error_with_one_call(value):
    model = judge(with_rationale(value))
    result = judge_synthesis(model, SAMPLE, stored())
    assert result.status == "error" and result.score is None and result.scores is None
    assert result.rationale == "" and "invalid judge reply" in result.reason
    assert len(model.seen) == 1  # one call, never retried


def test_a_judge_call_that_raises_is_an_error():
    def boom(messages):
        raise RuntimeError("provider unavailable")
    result = judge_synthesis(ScriptedChat(reply=boom), SAMPLE, stored())
    assert result.status == "error" and "provider unavailable" in result.reason


def test_without_a_judge_synthesis_is_not_judged():
    result = not_judged("--judge-provider none")
    assert result.status == "not_judged" and result.score is None and result.scores is None


# ---- T18 CS5: the judge is told the limit it is held to; a refusal says why -----------------

def test_the_prompt_states_the_600_character_limit_and_has_its_own_version():
    from insightforge_agent.evaluation.judge import JUDGE_PROMPT_VERSION, MAX_RATIONALE_CHARS, SYSTEM

    assert MAX_RATIONALE_CHARS == 600 and "at most 600 characters" in SYSTEM
    assert JUDGE_PROMPT_VERSION == "judge-prompt-2"
    assert RUBRIC_VERSION == "synthesis-rubric-v1"  # the scoring rubric itself is unchanged
    for text in CRITERIA.values():
        assert text in SYSTEM


@pytest.mark.parametrize("reply, error_type", [
    (with_rationale("x" * 601), "string_too_long"),
    (json.dumps(json.loads(sheet()) | {"accuracy": 5}), "extra_forbidden"),
    ('{"coverage": 4, "organisation": 4, "synthesis": 4, "calibration": 4, "rationale": "cut',
     "json_invalid"),
], ids=["over-long rationale", "extra key", "cut off"])
def test_an_invalid_reply_keeps_its_bounded_validation_error_type_and_length(reply, error_type):
    result = judge_synthesis(judge(reply), SAMPLE, stored())
    assert result.status == "error" and result.score is None and result.scores is None
    assert error_type in result.reason and f"{len(reply)} chars" in result.reason
    assert len(result.reason) < 1000  # bounded: head and tail only, never the whole reply
