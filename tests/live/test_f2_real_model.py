"""F2 real-model validation: the 16-case F2 matrix through the real `fact_check()` with the
production embedder (bge-small, local cache) and the configured production light model.

Paid: about 14 light-model calls (the two wrong-year cases stop at the numeric gate). Run only
this file, with both opt-ins and the provider that was validated:

    LLM_PROVIDER=anthropic INSIGHTFORGE_ALLOW_LIVE_TESTS=1 \
        uv run pytest tests/live/test_f2_real_model.py -m live -s

Each case runs once; nothing is retried. Every case is recorded (verdict, model called, judged
spans, similarity, entailment verdicts, raw reply, tokens) before any assertion, so one miss does
not hide the rest. A case passes only if:
- the verdict is the expected one, and the model was called exactly when the matrix says;
- every call returned a usable judgment (no provider error, no parse failure), so a fail-closed
  `unverified` from an outage can never count as a rejection;
- a verified Claim was SUPPORTED on a span containing the sentence that states it.
Set F2_LIVE_REPORT to a path to also write the record as JSON."""

import json
import os
import re

import pytest
from langchain_core.callbacks import BaseCallbackHandler

from insightforge_agent.config import Settings
from insightforge_agent.llm import build_chat_model
from insightforge_agent.pipeline.fact_check import fact_check
from tests.integration.conftest import embedder, offline_models  # noqa: F401 - fixtures
from tests.integration.test_f2_fact_check_bge import CASES
from tests.pipeline.conftest import make_deps  # noqa: F401 - shared fixture
from tests.pipeline.test_t6_writer_fact_check import URL, Fetcher, draft_of, evidence

pytestmark = pytest.mark.live


class Recorder(BaseCallbackHandler):
    """Records each model call's prompt, raw reply, token usage and any provider error."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.calls.append({"prompt": str(messages[0][-1].content)})

    def on_llm_end(self, response, **kwargs):
        message = response.generations[0][0].message
        self.calls[-1].update(reply=message.text, usage=dict(message.usage_metadata or {}))

    def on_llm_error(self, error, **kwargs):
        self.calls[-1]["error"] = f"{type(error).__name__}: {error}"[:300]


def numbered(prompt: str) -> list[str]:
    return re.findall(r"^\[\d+\] (.+)$", prompt, re.MULTILINE)


def judged(calls: list[dict], events: list) -> list[tuple[str, list[str]]]:
    """(span text, verdicts) for every span shown, pairing each call with its reply event."""
    replies = [e.payload["verdicts"] for e in events if e.type == "fact_check_entailment"]
    return [(span, verdicts.get(str(n), []))
            for call, verdicts in zip(calls, replies, strict=False)
            for n, span in enumerate(numbered(call["prompt"]), start=1)]


def test_f2_matrix_with_the_production_light_model(make_deps, embedder):  # noqa: F811
    settings = Settings()
    recorder = Recorder()
    light = build_chat_model("light", settings)
    model_name = getattr(light, "model", None) or getattr(light, "model_name", "?")
    rows = []
    for case in CASES:
        claim, page, support, expected, reaches_judge = case.values
        deps = make_deps(fetcher=Fetcher({URL: page}), embedder=embedder,
                         light_model=light.with_config(callbacks=[recorder]))
        _, bundle = evidence(deps, [page])
        before = len(recorder.calls)
        (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
        calls = recorder.calls[before:]
        events = [e for e in deps.repos.events.read_after("alice", "run_1")
                  if e.type.startswith("fact_check_entailment")]
        failures = [e.payload["error"] for e in events
                    if e.type == "fact_check_entailment_failed"]
        spans = judged(calls, events)
        setup_ok = v.check == "web_refetch" and v.matched is not None and len(
            deps.repos.passages.list("alice", v.matched.observation_id)) == 1
        judgment_ok = len(spans) == sum(len(numbered(c["prompt"])) for c in calls) and not (
            failures or any("error" in c for c in calls))
        evidence_ok = expected != "verified" or any(
            support in text and "SUPPORTED" in verdicts for text, verdicts in spans)
        rows.append({
            "case": case.id, "expected": expected, "actual": v.verdict,
            "model_called": bool(calls), "expected_model_call": reaches_judge,
            "similarity": round(v.similarity or 0.0, 3), "reason": v.reason,
            "judged": [{"span": t, "verdicts": vs} for t, vs in spans],
            "failures": failures, "raw_replies": [c.get("reply") for c in calls],
            "errors": [c["error"] for c in calls if "error" in c],
            "usage": [c.get("usage") for c in calls],
            "setup_ok": setup_ok, "judgment_ok": judgment_ok, "evidence_ok": evidence_ok,
            "pass": (setup_ok and judgment_ok and evidence_ok and v.verdict == expected
                     and bool(calls) == reaches_judge),
        })

    usage = [u for r in rows for u in r["usage"] if u]
    summary = {
        "provider": settings.llm_provider, "light_model": model_name,
        "embedding_model": settings.embedding_model,
        "model_calls": sum(len(r["raw_replies"]) for r in rows),
        "errors": sum(len(r["errors"]) for r in rows),
        "parse_failures": sum(1 for r in rows for f in r["failures"]
                              if f.startswith("BadModelReply")),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "passed": sum(r["pass"] for r in rows), "cases": len(rows),
    }
    print("\n" + json.dumps(summary, indent=2))
    for r in rows:
        print(f"{'PASS' if r['pass'] else 'FAIL'} {r['case']}: expected={r['expected']} "
              f"actual={r['actual']} called={r['model_called']} sim={r['similarity']} "
              f"failures={r['failures']} reason={r['reason']!r}")
        for j in r["judged"]:
            print(f"      judged {j['verdicts']}: {j['span']}")
    if os.environ.get("F2_LIVE_REPORT"):
        with open(os.environ["F2_LIVE_REPORT"], "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "cases": rows}, f, indent=2)
    assert summary["errors"] == summary["parse_failures"] == 0
    assert [r["case"] for r in rows if not r["pass"]] == []
