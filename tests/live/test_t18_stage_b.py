"""T18 Stage B (spec v3.1 section 5): paid, small calibrations on the saved T18 data. NOT run
without the owner's authorisation and a budget for each part. Each part runs alone:

    T18_EVAL_DB=<T18-evidence-package/raw/eval.db> T18_STAGE_B_OUT=<dir> LLM_PROVIDER=anthropic \
    INSIGHTFORGE_ALLOW_LIVE_TESTS=1 uv run pytest tests/live/test_t18_stage_b.py -m live -s -k b1
    (... -k b2 for the drafting smoke test; Stage B-3 is `insightforge-agent eval-judge`)

Stored data only: no re-fetch, no search. The SDK's own retries are off and nothing is retried.
Before each call a guard adds the provider-reported usage of the calls made so far, at the
published Claude Haiku 5.5 prices, to the next call's worst case (estimated input plus
max_tokens) and stops the part if that would pass its allocation. This bounds what the harness
sends; it is not a billing control (check the Console balance before and after).

The pass rules are fixed in advance in tests/t18_acceptance.py and unit-tested offline:
B-1 (`b1_failures`): no adversarial negative judged SUPPORTED; each of the 8 positives
adjudicated clearly supported judged SUPPORTED (the partly supported Germany Claim and the
doubtful H20 Claim are reported, not gated); none of T18's 27 verified Claims lost; no case
missing. One run is a minimum regression gate, not statistical proof.
B-2 (`b2_failures`): for every cap configuration (None, 150, 120) independently, no reply that
is not the schema unless the provider stopped it at max_tokens (truncation, reported). Each
configuration's results and the cap decision (`b2_cap_decision`) are recorded."""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from langchain_core.callbacks import BaseCallbackHandler

from insightforge_agent.agents.llm_json import BadModelReply, StructuredOutputRejected, ask_structured
from insightforge_agent.config import Settings
from insightforge_agent.domain.contracts import UNVERIFIED_MARK
from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import batch_passages, render_batch, split_by_passages
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.llm import build_chat_model, native_json
from insightforge_agent.pipeline.entailment import ENTAIL_SYSTEM
from insightforge_agent.pipeline.synthesize import DRAFT_SYSTEM, _Drafts
from tests.t18_acceptance import b1_failures, b2_cap_decision, b2_failures, b2_summary

pytestmark = pytest.mark.live
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "t18"
PRICES = {"input": 0.10 / 1e6, "output": 0.50 / 1e6}  # Claude Haiku 5.5, published
ALLOCATION = {"b1": 0.10, "b2": 0.15}


class Budget(BaseCallbackHandler):
    """Provider-reported usage so far, and a stop before a call that could pass the allocation."""

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


def model_and_budget(part: str):
    model = build_chat_model("light", Settings()).model_copy(update={"max_retries": 0})
    budget = Budget(ALLOCATION[part], model.max_tokens)
    return model.with_config(callbacks=[budget]), model, budget


def t18_db() -> sqlite3.Connection:
    path = os.environ.get("T18_EVAL_DB")
    if not path or not Path(path).is_file():
        pytest.skip("T18_EVAL_DB is not set to the saved T18 eval.db")
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


def test_b1_entailment_calibration():
    from insightforge_agent.pipeline.fact_check import counterpart, screen

    con = t18_db()
    cases = json.loads((FIXTURES / "stage_b1_cases.json").read_text(encoding="utf-8"))
    bound, _, budget = model_and_budget("b1")
    deps = checker_deps(bound)
    stored = verdicts(con)
    record = {"positives": [], "negatives": [], "verified": []}
    for g in cases["groups"]:
        v, _ = stored[g["ref"]]
        cited = next(p for p in passages(con, v["original"]["observation_id"])
                     if p.index == v["original"]["index"])
        found, match = counterpart(cited, passages(con, v["matched"]["observation_id"]))
        assert found is not None, (g["ref"], match)
        for case in [{"id": "P", "kind": "positive", "text": g["positive"]}] + g["negatives"]:
            s = screen(deps, case["text"], [found], floor=-1.0)
            outcome = (judged(deps, budget, case["text"], s.candidates) if s.candidates
                       else f"blocked before entailment: {s.reason}")
            row = {"ref": g["ref"], "id": case["id"], "kind": case["kind"], "outcome": outcome}
            if case["id"] == "P":
                row["adjudication"] = cases["adjudication"][g["ref"]]
                record["positives"].append(row)
            else:
                record["negatives"].append(row)
    for key, (v, claim) in stored.items():
        if v["verdict"] != "verified":
            continue
        s = screen(deps, claim, passages(con, v["matched"]["observation_id"]))
        outcome = judged(deps, budget, claim, s.candidates) if s.candidates else "no candidate"
        record["verified"].append({"ref": key, "outcome": outcome})
    record["spent_estimate_usd"], record["calls"] = round(budget.spent, 4), budget.calls
    write("b1", record)
    failures = b1_failures(record, cases)
    assert not failures, "\n".join(failures)


def test_b2_drafting_smoke():
    con = t18_db()
    spec = json.loads((FIXTURES / "stage_b2_batches.json").read_text(encoding="utf-8"))
    refs = list(dict.fromkeys(tuple(r) for b in spec["batches"] for r in b))
    by_obs = {}
    for obs, _ in refs:
        by_obs.setdefault(obs, {p.index: p for p in passages(con, obs)})
    ordered = [by_obs[o][i] for o, i in refs]
    bound, _, budget = model_and_budget("b2")
    native = native_json(bound, _Drafts)
    record = {}
    for cap in (None, 150, 120):
        parts = [p for b in batch_passages(ordered, 3000) for p in split_by_passages(b, cap)]
        rows = []
        for part in parts:
            user = f"Sub-task: {spec['query']}\n\n{render_batch(part)}"
            if not budget.allow(DRAFT_SYSTEM + user):
                pytest.fail(f"stopped by the budget guard after ${budget.spent:.4f}")
            try:
                items = len(ask_structured(bound, DRAFT_SYSTEM, user, _Drafts, native).items)
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
    record["summary"], record["cap_decision"] = b2_summary(record), b2_cap_decision(record)
    write("b2", record)
    failures = b2_failures(record)
    assert not failures, "\n".join(failures)
