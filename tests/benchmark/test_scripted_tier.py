"""Tier 3: scripted provider responses (tests/scripted.py). These check the production
orchestration around a model: parsing, invalid output and failure handling. A scripted reply
validates plumbing only. It is never a prediction, and nothing here reaches a benchmark Verdict."""

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.pipeline.entailment import EntailmentVerdicts
from insightforge_agent.pipeline.extract import extract
from tests.pipeline.test_extraction_synthesis import (  # noqa: F401 - `world` is a fixture
    all_supported,
    draft_all,
    light,
    world,
)
from tests.pipeline.conftest import make_deps  # noqa: F401 - a fixture `world` needs
from tests.scripted import ScriptedChat

pytestmark = pytest.mark.scripted

REPLY = '{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}'


def model(*replies: str) -> ScriptedChat:
    return ScriptedChat(script=[AIMessage(content=r) for r in replies])


# ---- parsing and invalid output --------------------------------------------------------------

def test_a_valid_reply_parses_and_a_fenced_one_too():
    assert ask_json(model(REPLY), "s", "u", EntailmentVerdicts).verdicts[0].verdict == "SUPPORTED"
    fenced = f"```json\n{REPLY}\n```"
    assert ask_json(model(fenced), "s", "u", EntailmentVerdicts).verdicts[0].passage == 1


@pytest.mark.parametrize("reply", [
    "not json at all", "", '{"verdicts": "nope"}', '{"verdicts": [{"passage": "x"}]}',
    '{"verdicts": [{"passage": 1, "verdict": "MAYBE"}]}',
])
def test_an_invalid_reply_raises_bad_model_reply(reply):
    with pytest.raises(BadModelReply, match="EntailmentVerdicts"):
        ask_json(model(reply), "s", "u", EntailmentVerdicts)


def test_a_scripted_model_called_more_often_than_scripted_fails_loudly():
    chat = model(REPLY)
    ask_json(chat, "s", "u", EntailmentVerdicts)
    with pytest.raises(AssertionError, match="more times than scripted"):
        ask_json(chat, "s", "u", EntailmentVerdicts)


# ---- failure handling in the production stage ------------------------------------------------

def plan_and_research(world, text="BYD sold 4.27 million vehicles in 2025."):
    world.page("https://a.test/x", text)
    return world.plan(("s1", "a1", 1)), world.research(s1=["https://a.test/x"])


def test_an_unparseable_extraction_reply_is_logged_and_its_passages_reported_unread(world):
    plan, research = plan_and_research(world)
    world.deps.light_model = model("this is not json")
    assert world.items(world.synth(plan, research)) == []
    invalid, exhausted = world.events("work_unit")
    assert (invalid["status"], exhausted["status"]) == ("invalid", "exhausted")
    assert invalid["detail"].startswith("_Facts: ") and invalid["stage"] == "extraction"
    (done,) = world.events("extraction_source_done")
    assert done["failed_units"][0]["reason"] == "invalid"


CUT_OFF = {"response_metadata": {"stop_reason": "max_tokens"},
           "usage_metadata": {"input_tokens": 1, "output_tokens": 4096, "total_tokens": 4097,
                              "output_token_details": {"reasoning": 4000}}}


def test_a_cut_off_extraction_reply_logs_why_it_failed(world):
    """F3: the logged detail says whether the JSON stopped early and how the provider stopped."""
    sid = world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.")
    world.deps.light_model = ScriptedChat(script=[AIMessage(content='{"facts": [', **CUT_OFF)])
    result = extract(world.deps, "alice", "run1", [sid])
    assert result.facts == [] and result.failed_units[0].reason == "truncated"
    (event, _) = world.events("work_unit")
    assert event["source_id"] == sid and event["status"] == "truncated"
    assert event["detail"].startswith("_Facts: incomplete_json, 1 problem(s) [json_invalid")
    assert "stop_reason=max_tokens; output_tokens=4096 (reasoning 4000)" in event["detail"]
    assert "11 chars" in event["detail"]


def test_an_unparseable_support_reply_is_logged_and_the_item_is_dropped(world):
    plan, research = plan_and_research(world)
    world.deps.light_model = light(draft=draft_all(), entail=lambda ps, u: "not a list")
    assert world.items(world.synth(plan, research)) == []
    (judged,) = world.events("support_judged")
    assert judged["error"] and judged["judgments"][0]["verdict"] == "VERIFICATION_ERROR"
    assert world.events("evidence_dropped")[0]["reason"] == "no supported Passage"


def test_a_scripted_support_verdict_cannot_rescue_a_failed_literal_check(world):
    """The real drop outcome is preserved: a Passage that fails the figure check never reaches
    the support judgment, whatever the scripted model would have said."""
    plan, research = plan_and_research(world, "BYD sold 4.27 million vehicles in 2025.")
    chat = light(draft=lambda ps, u: [
        {"statement": "BYD sold 9.99 million vehicles in 2025.", "passages": [n]} for n, _ in ps],
        entail=all_supported)
    world.deps.light_model = chat
    assert world.items(world.synth(plan, research)) == []
    assert chat.calls("You check whether") == []
    dropped = world.events("evidence_dropped")[0]
    assert dropped["reason"].startswith("no Passage passes the provenance and literal-value")
    assert "value not in Passage" in dropped["rejected"][0]
