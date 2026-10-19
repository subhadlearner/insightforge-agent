"""What the paid T18 Stage B harness (tests/live/test_t18_stage_b.py) runs, kept outside
tests/live so it is exercised offline (tests/test_t18_stage_b_support.py): the models, the
spending guard, the saved-data access and the B-1 entailment step. No model is called here."""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from langchain_core.callbacks import BaseCallbackHandler

from insightforge_agent.config import Settings
from insightforge_agent.domain.contracts import UNVERIFIED_MARK
from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.llm import build_chat_model, native_json
from insightforge_agent.pipeline.entailment import ENTAIL_SYSTEM
from insightforge_agent.pipeline.synthesize import _Drafts

PRICES = {"input": 0.10 / 1e6, "output": 0.50 / 1e6}  # Claude Haiku 5.5, published
ALLOCATION = {"b1": 0.10, "b2": 0.15}


class Budget(BaseCallbackHandler):
    """Provider-reported usage so far, and a stop before a call that could pass the allocation.
    It bounds what the harness sends; it is not a billing control."""

    def __init__(self, allocation: float, max_tokens: int) -> None:
        self.allocation, self.max_tokens, self.spent, self.calls = allocation, max_tokens, 0.0, 0

    def on_llm_end(self, response, **kwargs):
        usage = getattr(response.generations[0][0].message, "usage_metadata", None) or {}
        self.spent += (usage.get("input_tokens", 0) * PRICES["input"]
                       + usage.get("output_tokens", 0) * PRICES["output"])
        self.calls += 1

    def allow(self, input_text: str) -> bool:
        worst = (estimate_tokens(input_text) * PRICES["input"]
                 + self.max_tokens * PRICES["output"])
        return self.spent + worst <= self.allocation


def light_model(settings: Settings | None = None):
    """The production light model with the SDK's own retries off: one call, one request."""
    return build_chat_model("light", settings or Settings()).model_copy(update={"max_retries": 0})


def checker_model(part: str, settings: Settings | None = None):
    """B-1: the light model, metered (plain JSON, as the production entailment step uses)."""
    model = light_model(settings)
    budget = Budget(ALLOCATION[part], model.max_tokens)
    return model.with_config(callbacks=[budget]), budget


def drafting_runner(part: str, settings: Settings | None = None):
    """B-2: the production native structured output for drafting, built on the chat model
    itself, with the meter attached to the runnable that makes each call. Refuses to run when
    native output is not active, so B-2 can never test plain JSON by mistake."""
    model = light_model(settings)
    native = native_json(model, _Drafts)
    if native is None:
        raise RuntimeError(f"native structured output is not active for {type(model).__name__}; "
                           "Stage B-2 tests only the native path")
    budget = Budget(ALLOCATION[part], model.max_tokens)
    return model, native.with_config(callbacks=[budget]), budget


def require_t18_db() -> sqlite3.Connection:
    """The saved T18 eval.db, read-only. Stage B runs only when explicitly enabled, so a
    missing database fails the run instead of skipping it as if it had passed."""
    path = os.environ.get("T18_EVAL_DB")
    if not path or not Path(path).is_file():
        pytest.fail("T18_EVAL_DB must name the saved T18 eval.db for Stage B")
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def write(name: str, record) -> None:
    print(f"\nT18_STAGE_B {name} " + json.dumps(record, indent=1)[:4000])
    if out := os.environ.get("T18_STAGE_B_OUT"):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{name}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def passages(con, observation_id) -> list[Passage]:
    return [Passage(observation_id=observation_id, index=i, text=t) for i, t in con.execute(
        "SELECT idx, text FROM passages WHERE observation_id=? ORDER BY idx", (observation_id,))]


def verdicts(con) -> dict[str, tuple[dict, str]]:
    """'run section.claim' -> (stored verdict, Claim text)."""
    out = {}
    for run_id, body in con.execute("SELECT run_id, body FROM reports"):
        report = json.loads(body)
        for v in report["verified"]["verdicts"]:
            claim = report["draft"]["sections"][v["section"]]["claims"][v["claim"]]["text"]
            out[f"{run_id} {v['section']}.{v['claim']}"] = (
                v, claim.replace(UNVERIFIED_MARK, "").strip())
    return out


def checker_deps(model):
    from insightforge_agent.embeddings import FastEmbedEmbedder
    from insightforge_agent.pipeline.deps import Deps
    from insightforge_agent.stores.memory import MemoryRepos

    return Deps(repos=MemoryRepos(), store=None, planner_model=None, writer_model=None,
                light_model=model, researcher_model=None, search=None, fetcher=None,
                checkpointer=None, embedder=FastEmbedEmbedder("BAAI/bge-small-en-v1.5"))


def judged(deps, budget, claim: str, candidates) -> str:
    """One entailment call through the production step 4; its outcome as a word."""
    from insightforge_agent.pipeline.fact_check import _entailed

    if not budget.allow(ENTAIL_SYSTEM + claim + " ".join(s.text for _, s in candidates)):
        pytest.fail(f"stopped by the budget guard after ${budget.spent:.4f}")
    supported, reason = _entailed(deps, "t18-stage-b", "b", claim, candidates, section=0, claim=0)
    return "SUPPORTED" if supported else f"not supported: {reason}"


def run_b2(texts: dict, spec: dict, model, native, budget) -> dict:
    """The B-2 drafting smoke test proper: the spec's batches (Passage refs; `texts` maps each
    (observation_id, index) to its Passage), re-batched with no cap, 150 and 120, each part
    drafted once through the production `ask_structured` on `native`. Records every reply and
    that the native path was the one used; never retries."""
    from insightforge_agent.agents.llm_json import (
        BadModelReply,
        StructuredOutputRejected,
        ask_structured,
    )
    from insightforge_agent.domain.passages import batch_passages, render_batch, split_by_passages
    from insightforge_agent.pipeline.synthesize import DRAFT_SYSTEM

    refs = list(dict.fromkeys(tuple(r) for b in spec["batches"] for r in b))
    ordered = [Passage(observation_id=o, index=i, text=texts[(o, i)]) for o, i in refs]
    record = {"native_structured_output": native is not None}
    for cap in (None, 150, 120):
        parts = [p for b in batch_passages(ordered, 3000) for p in split_by_passages(b, cap)]
        rows = []
        for part in parts:
            user = f"Sub-task: {spec['query']}\n\n{render_batch(part)}"
            if not budget.allow(DRAFT_SYSTEM + user):
                pytest.fail(f"stopped by the budget guard after ${budget.spent:.4f}")
            try:
                items = len(ask_structured(model, DRAFT_SYSTEM, user, _Drafts, native).items)
                rows.append({"passages": len({(w.passage.observation_id, w.passage.index)
                                              for w in part}), "items": items})
            except BadModelReply as e:
                d = e.diagnostic
                rows.append({"kind": d.kind, "stop_reason": d.stop_reason,
                             "output_tokens": d.output_tokens})
            except StructuredOutputRejected as e:
                rows.append({"kind": "rejected", "detail": str(e)})
        record[str(cap)] = rows
    record["spent_estimate_usd"], record["calls"] = round(budget.spent, 4), budget.calls
    return record
