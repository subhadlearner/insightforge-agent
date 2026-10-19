"""R3-01: the paid Stage B-2 harness really drafts through Anthropic native structured output,
its meter sees every call, and its guard stops before the allocation could be passed. Offline:
the real installed ChatAnthropic with only its HTTP call replaced; no provider is reached."""

import json
import os
from pathlib import Path

import pytest
from anthropic.types import Message, TextBlock, Usage
from langchain_anthropic import ChatAnthropic

from insightforge_agent.config import Settings
from tests.t18_stage_b_support import (
    PRICES,
    Budget,
    checker_model,
    drafting_runner,
    require_out,
    require_t18_db,
    run_b2,
    run_identity,
    settle,
    write,
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
    assert budget.calls == 3
    assert budget.spent == pytest.approx(3 * (900 * PRICES["input"] + 40 * PRICES["output"]))
    # Every call is kept as the provider reported it, raw reply included.
    assert [(c["input_tokens"], c["output_tokens"], c["stop_reason"]) for c in budget.log] == [
        (900, 40, "end_turn")] * 3
    assert all(c["model"] == "claude-haiku-5-5" and '"items"' in c["reply"] for c in budget.log)


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


# ---- What a paid run keeps: every result, every call, and what produced them ----------------

def test_a_run_stopped_by_the_guard_keeps_what_it_did_and_paid_for(anthropic_http):
    model, native, budget = drafting_runner("b2", SETTINGS)
    answers = iter([True, False])  # the guard allows the first call, then stops
    budget.allow = lambda _: next(answers)
    record = {}
    with pytest.raises(pytest.fail.Exception, match="stopped by the budget guard"):
        run_b2(TEXTS, SPEC, model, native, budget, record)
    assert len(anthropic_http) == 1
    assert record["None"] == [{"passages": 2, "items": 1}] and record["150"] == []
    settled = settle(record, budget, {"part": "b2"})
    assert settled["calls"] == 1 and len(settled["call_log"]) == 1
    assert settled["spent_estimate_usd"] == round(900 * PRICES["input"] + 40 * PRICES["output"], 4)


def test_the_b1_checker_logs_each_call_and_each_provider_error(anthropic_http, monkeypatch):
    bound, budget = checker_model("b1", SETTINGS)
    bound.invoke("Is the Claim supported?")
    assert budget.calls == 1 and budget.log[0]["stop_reason"] == "end_turn"

    def refuse(self, payload):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(ChatAnthropic, "_create", refuse)
    with pytest.raises(RuntimeError):
        bound.invoke("Is the Claim supported?")  # max_retries=0: one request, no retry
    assert budget.log[1] == {"error": "RuntimeError: overloaded"} and budget.calls == 1


def test_a_record_names_the_code_model_settings_and_data_that_produced_it(tmp_path, monkeypatch):
    import hashlib
    import subprocess

    db = tmp_path / "eval.db"
    db.write_bytes(b"saved T18 data")
    monkeypatch.setenv("T18_EVAL_DB", str(db))
    bound, _ = checker_model("b1", SETTINGS)
    identity = run_identity("b1", bound.bound)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1]).stdout
    assert identity["commit"] == head.strip() and isinstance(identity["dirty"], bool)
    assert (identity["model"], identity["max_retries"], identity["max_tokens"]) == (
        "claude-haiku-5-5", 0, 4096)
    assert identity["allocation_usd"] == 0.10
    assert identity["eval_db_sha256"] == hashlib.sha256(b"saved T18 data").hexdigest()


def test_stage_b_needs_an_output_directory_before_any_call(tmp_path, monkeypatch):
    monkeypatch.delenv("T18_STAGE_B_OUT", raising=False)
    with pytest.raises(pytest.fail.Exception, match="T18_STAGE_B_OUT"):
        require_out()
    with pytest.raises(pytest.fail.Exception, match="T18_STAGE_B_OUT"):
        write("b1", {"x": 1})
    monkeypatch.setenv("T18_STAGE_B_OUT", str(tmp_path / "out"))
    write("b1", {"x": 1})
    assert json.loads((tmp_path / "out" / "b1.json").read_text(encoding="utf-8")) == {"x": 1}
