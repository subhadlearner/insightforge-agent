"""F3: a failed JSON reply carries a bounded diagnostic that says why it failed
(docs/audits/2026-10-12-f3-json-replies.md). Synthetic replies only; no model is called."""

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, Usage
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from insightforge_agent.agents.llm_json import (
    HEAD_CHARS,
    TAIL_CHARS,
    BadModelReply,
    ask_json,
)
from insightforge_agent.config import Settings
from insightforge_agent.llm import build_chat_model
from tests.scripted import ScriptedChat

pytestmark = pytest.mark.scripted


class Items(BaseModel):
    items: list[int]


def chat(content, **metadata) -> ScriptedChat:
    return ScriptedChat(script=[AIMessage(content=content, **metadata)])


def failure(content, **metadata) -> BadModelReply:
    with pytest.raises(BadModelReply) as info:
        ask_json(chat(content, **metadata), "s", "u", Items)
    return info.value


ANTHROPIC_CUT = {"response_metadata": {"stop_reason": "max_tokens"},
                 "usage_metadata": {"input_tokens": 900, "output_tokens": 4096, "total_tokens": 4996,
                                    "output_token_details": {"reasoning": 3900}}}


# ---- success is unchanged ---------------------------------------------------------------------

def test_a_valid_reply_and_a_fenced_one_parse_as_before():
    assert ask_json(chat('{"items": [1, 2]}'), "s", "u", Items) == Items(items=[1, 2])
    assert ask_json(chat('```json\n{"items": [3]}\n```'), "s", "u", Items) == Items(items=[3])


def test_metadata_on_a_valid_reply_changes_nothing():
    assert ask_json(chat('{"items": [1]}', **ANTHROPIC_CUT), "s", "u", Items) == Items(items=[1])


# ---- what kind of failure ---------------------------------------------------------------------

@pytest.mark.parametrize("text, kind, types", [
    ('{"items": [1, 2', "incomplete_json", ("json_invalid",)),
    ('{"items": [1], "note": "half a str', "incomplete_json", ("json_invalid",)),
    ("not json", "invalid_json", ("json_invalid",)),
    ('{"items": [1,]}', "invalid_json", ("json_invalid",)),
    ('{"items": [1]} and some prose', "invalid_json", ("json_invalid",)),
    ('{"items": "x"}', "schema", ("list_type",)),
    ('{"items": ["a", "b"], "n": 1}', "schema", ("int_parsing",)),
    ("{}", "schema", ("missing",)),
    ("", "empty", ("json_invalid",)),
    ("```json\n```", "empty", ("json_invalid",)),
])
def test_incomplete_malformed_schema_invalid_and_empty_replies_are_told_apart(text, kind, types):
    e = failure(text)
    d = e.diagnostic
    assert (d.schema, d.kind, d.error_types) == ("Items", kind, types)
    assert str(e).startswith(f"Items: {kind}, ")
    assert d.error_count >= 1 and d.error


def test_a_schema_failure_counts_every_problem_and_names_each_type_once():
    d = failure('{"items": ["a", "b", 3, "c"]}').diagnostic
    assert d.kind == "schema" and d.error_count == 3 and d.error_types == ("int_parsing",)


def test_the_parser_message_is_kept_for_an_incomplete_reply():
    d = failure('{"items": [1, 2').diagnostic
    assert "EOF while parsing a list" in d.error


# ---- bounded: prefix and suffix, never the whole reply ----------------------------------------

def test_a_huge_reply_keeps_only_a_bounded_head_and_tail():
    middle = "MIDDLE-OF-THE-REPLY " * 5000
    text = '{"items": [1, 2, 3], "note": "' + middle + 'the very end'
    e = failure(text)
    d = e.diagnostic
    assert d.length == len(text)
    assert d.head == text[:HEAD_CHARS] and d.tail == text[-TAIL_CHARS:]
    assert d.tail.endswith("the very end") and d.head.startswith('{"items": [1, 2, 3]')
    rendered = str(e)
    assert len(rendered) < HEAD_CHARS + TAIL_CHARS + 600
    assert rendered.count("MIDDLE-OF-THE-REPLY") < 30 and "the very end" in rendered
    assert f"{len(text)} chars" in rendered


def test_no_reference_to_the_full_reply_survives_on_the_exception():
    text = '{"items": [' + "1, " * 50_000
    e = failure(text)
    assert e.__cause__ is None and e.__context__ is None
    assert all(len(str(v)) < len(text) for v in vars(e).values())
    assert all(len(str(v)) < 1000 for v in vars(e.diagnostic).values())


def test_a_short_reply_is_shown_once_not_twice():
    e = failure('{"items": [1, 2')
    assert e.diagnostic.head == '{"items": [1, 2' and e.diagnostic.tail == ""
    assert str(e).count('{"items": [1, 2') == 1


def test_head_and_tail_do_not_overlap_just_above_the_head_size():
    text = '{"items": [' + "1, " * ((HEAD_CHARS + 50) // 3)
    d = failure(text).diagnostic
    assert d.head + d.tail == text


# ---- provider metadata ------------------------------------------------------------------------

def test_the_stop_reason_and_output_tokens_are_recorded_when_the_provider_reports_them():
    e = failure('{"items": [1, 2', **ANTHROPIC_CUT)
    d = e.diagnostic
    assert (d.stop_reason, d.output_tokens, d.reasoning_tokens) == ("max_tokens", 4096, 3900)
    assert "stop_reason=max_tokens" in str(e) and "output_tokens=4096 (reasoning 3900)" in str(e)


@pytest.mark.parametrize("metadata, reason", [
    ({"stop_reason": "end_turn"}, "end_turn"),          # Anthropic
    ({"finish_reason": "MAX_TOKENS"}, "MAX_TOKENS"),    # Gemini
    ({"finish_reason": "length"}, "length"),            # Groq / OpenAI-style
])
def test_a_finish_reason_is_read_under_either_common_name(metadata, reason):
    assert failure("nope", response_metadata=metadata).diagnostic.stop_reason == reason


def test_without_any_metadata_the_diagnostic_still_works():
    e = failure('{"items": [1, 2')
    d = e.diagnostic
    assert (d.stop_reason, d.output_tokens, d.reasoning_tokens) == (None, None, None)
    assert "stop_reason=not reported" in str(e) and "output_tokens=not reported" in str(e)


def test_output_tokens_without_a_reasoning_breakdown():
    e = failure("nope", usage_metadata={"input_tokens": 1, "output_tokens": 7, "total_tokens": 8})
    assert (e.diagnostic.output_tokens, e.diagnostic.reasoning_tokens) == (7, None)
    assert "output_tokens=7;" in str(e)


def test_the_configured_output_ceiling_is_recorded_when_the_model_exposes_one():
    model = chat("nope")
    assert getattr(model, "max_tokens", None) is None
    with pytest.raises(BadModelReply) as info:
        ask_json(model, "s", "u", Items)
    assert info.value.diagnostic.max_tokens is None


# ---- the real installed Anthropic integration, with the HTTP call replaced --------------------

def stubbed_anthropic(monkeypatch, content, stop_reason, output_tokens, thinking_tokens):
    model = build_chat_model("light", Settings(llm_provider="anthropic", anthropic_api_key="x",
                                                    tavily_api_key="t"))
    usage = Usage.model_validate({"input_tokens": 900, "output_tokens": output_tokens,
                                  "output_tokens_details": {"thinking_tokens": thinking_tokens}})
    message = Message(id="msg_offline", type="message", role="assistant", model=model.model,
                      content=content, stop_reason=stop_reason, stop_sequence=None, usage=usage)
    raw = type("Raw", (), {"parse": lambda self: message})()
    monkeypatch.setattr(type(model), "_create", lambda self, payload: raw)
    return model


@pytest.mark.parametrize("content", [
    [TextBlock(type="text", text='{"items": [1, 2')],
    [ThinkingBlock(type="thinking", thinking="...", signature="sig"),
     TextBlock(type="text", text='{"items": [1, 2')],
], ids=["text only", "with a thinking block"])
def test_the_installed_chat_anthropic_exposes_what_the_diagnostic_reads(monkeypatch, content):
    model = stubbed_anthropic(monkeypatch, content, "max_tokens", 4096, 3900)
    with pytest.raises(BadModelReply) as info:
        ask_json(model, "s", "u", Items)
    d = info.value.diagnostic
    assert (d.kind, d.stop_reason, d.output_tokens, d.reasoning_tokens, d.max_tokens) == (
        "incomplete_json", "max_tokens", 4096, 3900, 4096)
    assert d.head == '{"items": [1, 2'


def test_a_thinking_only_reply_is_empty_text(monkeypatch):
    model = stubbed_anthropic(
        monkeypatch, [ThinkingBlock(type="thinking", thinking="...", signature="sig")],
        "max_tokens", 4096, 4096)
    with pytest.raises(BadModelReply) as info:
        ask_json(model, "s", "u", Items)
    d = info.value.diagnostic
    assert (d.kind, d.length, d.stop_reason, d.reasoning_tokens) == ("empty", 0, "max_tokens", 4096)


# ---- the proposed live probe (tests/live/test_f3_json_probe.py) is set up right, offline ----

def test_the_f3_probe_fills_exactly_one_full_drafting_batch():
    from insightforge_agent.domain.passages import batch_passages, render_batch
    from insightforge_agent.domain.tokens import estimate_tokens
    from tests.live.test_f3_json_probe import probe_batch, probe_passages

    cap = Settings(llm_provider="fake", search_providers="fake").passage_token_cap
    batch = probe_batch(cap)
    assert 0.9 * cap < estimate_tokens(render_batch(batch)) <= cap
    assert len(batch_passages(probe_passages(), cap)) > 1  # the cap, not the text, ended it
    assert all(w.text == w.passage.text for w in batch)  # whole Passages, no windows


# ---- the Writer's model-facing feedback is unchanged ------------------------------------------

def test_the_short_form_given_back_to_the_writer_is_the_old_message():
    text = '{"items": "x"}'
    assert failure(text).brief == f"Items: 1 problem(s) in {text!r}"
    fenced = '```json\n{"items": "x"}\n```'
    assert failure(fenced).brief == f"Items: 1 problem(s) in {text!r}"
    long = '{"items": "' + "y" * 500 + '"}'
    assert failure(long).brief == f"Items: 1 problem(s) in {long[:200]!r}"
