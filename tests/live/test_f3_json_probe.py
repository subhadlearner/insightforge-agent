"""F3 probe: can one full drafting batch on the current light configuration stop at the output
ceiling? (docs/audits/2026-10-12-f3-json-replies.md, "Proposed live probe".)

Paid: exactly ONE light-model call. Not run without the owner's authorisation. Run only this
file, with both opt-ins and the provider under test:

    LLM_PROVIDER=anthropic INSIGHTFORGE_ALLOW_LIVE_TESTS=1 \
        uv run pytest tests/live/test_f3_json_probe.py -m live -s

The call is the drafting call as `_draft` makes it: DRAFT_SYSTEM, the "Sub-task: ..." user
message over `render_batch`, parsed by `ask_json` into `_Drafts`. The batch is one batch from
`batch_passages` at the configured `passage_token_cap`, filled with fixed, fact-dense synthetic
passages (fictional companies), so it is a worst case for reply length, not a replay: the
October 10 inputs did not survive. No web search, Planner, Researcher, Writer or Report.
Nothing is retried, whatever the outcome. The record (stop reason, output and reasoning tokens,
reply length, parse outcome, bounded head and tail) is printed; the probe asserts only its own
set-up and the call count, never the outcome."""

import json
import os

import pytest
from langchain_core.callbacks import BaseCallbackHandler

from insightforge_agent.agents.llm_json import HEAD_CHARS, TAIL_CHARS, BadModelReply, ask_json
from insightforge_agent.config import Settings
from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import batch_passages, render_batch
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.llm import build_chat_model
from insightforge_agent.pipeline.synthesize import DRAFT_SYSTEM, _Drafts

pytestmark = pytest.mark.live

QUERY = "Monthly battery electric vehicle deliveries and prices by maker and region, 2025"
MAKERS = ("Arvel Motors", "Brisa Auto", "Corvane EV", "Daltor Cars", "Esk Mobility",
          "Fenwick Electric", "Gorran Auto", "Hollis Motors")
REGIONS = ("China", "Europe", "North America")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def probe_passages() -> list[Passage]:
    """Fixed, fact-dense passages: every sentence states a distinct, checkable figure."""
    out = []
    for i, (maker, region) in enumerate((m, r) for m in MAKERS for r in REGIONS):
        sentences = []
        for j, month in enumerate(MONTHS):
            units = 1_000 + (i * 7_919 + j * 1_009) % 48_000
            price = 21_000 + (i * 1_543 + j * 377) % 30_000
            sentences.append(f"In {month} 2025, {maker} delivered {units:,} battery electric "
                             f"vehicles in {region} at an average price of ${price:,}.")
        out.append(Passage(observation_id="obs_f3_probe", index=i, text=" ".join(sentences)))
    return out


def probe_batch(token_cap: int):
    """The first batch `batch_passages` makes at the cap, as Synthesis would draft it."""
    return batch_passages(probe_passages(), token_cap)[0]


class Recorder(BaseCallbackHandler):
    """What the provider returned for each call, read the way ask_json's diagnostic reads it."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.calls.append({})

    def on_llm_end(self, response, **kwargs):
        m = response.generations[0][0].message
        usage = dict(m.usage_metadata or {})
        meta = m.response_metadata or {}
        self.calls[-1].update(
            stop_reason=meta.get("stop_reason") or meta.get("finish_reason"),
            output_tokens=usage.get("output_tokens"), input_tokens=usage.get("input_tokens"),
            reasoning_tokens=(usage.get("output_token_details") or {}).get("reasoning"),
            length=len(m.text), head=m.text[:HEAD_CHARS], tail=m.text[-TAIL_CHARS:])

    def on_llm_error(self, error, **kwargs):
        self.calls[-1]["error"] = f"{type(error).__name__}: {error}"[:300]


def test_f3_one_full_drafting_batch_on_the_light_model():
    settings = Settings()
    light = build_chat_model("light", settings)
    assert settings.llm_provider == "anthropic" and light.model == "claude-haiku-5-5"
    batch = probe_batch(settings.passage_token_cap)
    numbered = render_batch(batch)
    assert estimate_tokens(numbered) > 0.9 * settings.passage_token_cap  # a full batch

    recorder = Recorder()
    user = f"Sub-task: {QUERY}\n\n{numbered}"
    record = {"model": light.model, "max_tokens": light.max_tokens,
              "thinking": getattr(light, "thinking", None),
              "passage_token_cap": settings.passage_token_cap,
              "batch_passages": len(batch), "batch_estimated_tokens": estimate_tokens(numbered)}
    try:
        reply = ask_json(light.with_config(callbacks=[recorder]), DRAFT_SYSTEM, user, _Drafts)
        record.update(parsed=True, items=len(reply.items))
    except BadModelReply as e:
        record.update(parsed=False, diagnostic=str(e))
    assert len(recorder.calls) == 1  # one paid call, never retried
    record.update(recorder.calls[0])
    print("\nF3_PROBE " + json.dumps(record, indent=1))
    if os.environ.get("F3_PROBE_REPORT"):
        with open(os.environ["F3_PROBE_REPORT"], "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    assert "error" not in record, record["error"]
