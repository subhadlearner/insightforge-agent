"""R3-01: the paid Stage B-2 harness really drafts through Anthropic native structured output,
its meter sees every call, and its guard stops before the allocation could be passed. Offline:
the real installed ChatAnthropic with only its HTTP call replaced; no provider is reached."""

import json
import os

import pytest
from anthropic.types import Message, TextBlock, Usage
from langchain_anthropic import ChatAnthropic

from insightforge_agent.config import Settings
from tests.t18_stage_b_support import (
    PRICES,
    Budget,
    drafting_runner,
    require_t18_db,
    run_b2,
)

SETTINGS = Settings(llm_provider="anthropic", anthropic_api_key="x", tavily_api_key="t")
SPEC = {"query": "BYD sales", "batches": [[["o", 0], ["o", 1]]]}
TEXTS = {("o", 0): "BYD sold 4.27 million vehicles in 2025.",
         ("o", 1): "BYD sold 3.02 million vehicles in 2024."}
REPLY = json.dumps({"items": [{"statement": "BYD sold 4.27 million vehicles in 2025.",
                               "passages": [1]}]})


@pytest.fixture
def anthropic_http(monkeypatch):
    """Every request ChatAnthropic would send, answered with a fixed reply and usage."""
    sent = []

    def create(self, payload):
        sent.append(payload)
        message = Message(id="msg_offline", type="message", role="assistant", model=self.model,
                          content=[TextBlock(type="text", text=REPLY)], stop_reason="end_turn",
                          stop_sequence=None,
                          usage=Usage.model_validate({"input_tokens": 900, "output_tokens": 40}))
        return type("Raw", (), {"parse": lambda _: message})()

    monkeypatch.setattr(ChatAnthropic, "_create", create)
    return sent


def test_b2_drafts_through_native_structured_output_and_meters_every_call(anthropic_http):
    model, native, budget = drafting_runner("b2", SETTINGS)
    record = run_b2(TEXTS, SPEC, model, native, budget)
    assert record["native_structured_output"] is True
    assert len(anthropic_http) == 3  # one part per cap configuration: None, 150, 120
    for payload in anthropic_http:
        assert payload["output_config"]["format"]["type"] == "json_schema"  # native, not plain
        assert "items" in payload["output_config"]["format"]["schema"]["properties"]
        assert payload["max_tokens"] == 4096
    assert [r["items"] for cap in ("None", "150", "120") for r in record[cap]] == [1, 1, 1]
    assert budget.calls == record["calls"] == 3
    assert budget.spent == pytest.approx(3 * (900 * PRICES["input"] + 40 * PRICES["output"]))


def test_the_guard_stops_before_a_call_that_could_pass_the_allocation(anthropic_http):
    model, native, budget = drafting_runner("b2", SETTINGS)
    budget.allocation = 0.001  # below one worst-case call (4,096 output tokens = $0.002)
    with pytest.raises(pytest.fail.Exception, match="stopped by the budget guard"):
        run_b2(TEXTS, SPEC, model, native, budget)
    assert anthropic_http == []  # nothing was sent


def test_b2_refuses_to_run_without_native_output():
    fake = Settings(llm_provider="fake", tavily_api_key="t")
    with pytest.raises(RuntimeError, match="native structured output is not active"):
        drafting_runner("b2", fake)


def test_the_old_harness_shape_can_no_longer_fall_back_silently():
    """Before R3-01 the harness wrapped the model first (`with_config`) and then asked for native
    output, which quietly returned None; that shape now raises instead."""
    from insightforge_agent.llm import native_json
    from insightforge_agent.pipeline.synthesize import _Drafts
    from tests.t18_stage_b_support import light_model

    wrapped = light_model(SETTINGS).with_config(callbacks=[Budget(0.1, 4096)])
    with pytest.raises(TypeError, match="the chat model itself"):
        native_json(wrapped, _Drafts)


def test_stage_b_fails_rather_than_skips_without_the_t18_database(monkeypatch):
    monkeypatch.delenv("T18_EVAL_DB", raising=False)
    with pytest.raises(pytest.fail.Exception, match="T18_EVAL_DB"):
        require_t18_db()
    monkeypatch.setenv("T18_EVAL_DB", os.path.join(os.sep, "no", "such", "eval.db"))
    with pytest.raises(pytest.fail.Exception, match="T18_EVAL_DB"):
        require_t18_db()
