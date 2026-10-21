"""ADR-0006 live support gate: paid, small, on stored text only. NOT run without the owner's
separate authorisation and a spending limit. Run once, alone:

    T18_EVAL_DB=<T18 baseline eval.db> RESEARCH_GATE_OUT=<dir> RESEARCH_GATE_BUDGET_USD=<cap>
    LLM_PROVIDER=anthropic INSIGHTFORGE_ALLOW_LIVE_TESTS=1 \
    uv run pytest tests/live/test_research_integrity_live.py -m live -s

What it asks and how it is decided are fixed in tests/research_integrity_gate.py (unit-tested
offline; the dry run tests/integration/test_research_integrity_gate_plan.py builds every request
from the database without a call). Each judgment is one production `judge_units` call on one
cited unit. No search, no re-fetch, no web access beyond the model provider.

Spending bound: the SDK's own retries are off and nothing is retried. Before each call the T18
Stage B guard (`Budget`) adds the provider-reported usage so far, at the published Claude Haiku
5.5 prices, to the next call's worst case (estimated input plus max_tokens) and stops the run if
that would pass RESEARCH_GATE_BUDGET_USD, which is required and may not exceed $1.00. This
bounds what the harness sends; it is not a billing control (check the Console balance before
and after). A stop by the guard makes the result INCONCLUSIVE, never a pass."""

import json
import os
from pathlib import Path

import pytest

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.pipeline.support import (
    SUPPORT_PROMPT_VERSION,
    SUPPORT_SYSTEM,
    Unit,
    judge_units,
    render_request,
)
from tests.research_integrity_gate import b1_cases, decide, gold_cases, holdout_cases
from tests.t18_stage_b_support import Budget, checker_deps, light_model, require_t18_db

pytestmark = pytest.mark.live
MAX_ALLOCATION = 1.00


def allocation() -> float:
    raw = os.environ.get("RESEARCH_GATE_BUDGET_USD")
    if not raw:
        pytest.fail("RESEARCH_GATE_BUDGET_USD must state the authorised spending limit")
    value = float(raw)
    if not 0 < value <= MAX_ALLOCATION:
        pytest.fail(f"RESEARCH_GATE_BUDGET_USD must be above 0 and at most {MAX_ALLOCATION}")
    return value


def out_dir() -> Path:
    raw = os.environ.get("RESEARCH_GATE_OUT")
    if not raw:
        pytest.fail("RESEARCH_GATE_OUT must name the directory for the gate record")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    (path / ".write-check").write_text("ok", encoding="utf-8")
    (path / ".write-check").unlink()
    return path


def test_research_integrity_support_gate():
    cap, out = allocation(), out_dir()  # both before any call
    con = require_t18_db()
    repeats = int(os.environ.get("RESEARCH_GATE_REPEATS", "3"))
    assert 1 <= repeats <= 5
    cases = gold_cases(repeats) + b1_cases(con, repeats) + holdout_cases(repeats)
    model = light_model()
    budget = Budget(cap, model.max_tokens)
    deps = checker_deps(model.with_config(callbacks=[budget]))
    rows, stopped = [], None
    for c in cases:
        unit = Unit(PassageRef(observation_id=c.ref[0], index=c.ref[1]), c.passage, c.entity)
        for repeat in range(c.repeats):
            if not budget.allow(SUPPORT_SYSTEM + render_request(c.statement, [unit])):
                stopped = f"stopped by the budget guard after ${budget.spent:.4f}"
                break
            (j,) = judge_units(deps, "gate", "gate", c.statement, [unit], case=c.id)
            rows.append({"set": c.set, "id": c.id, "repeat": repeat, "verdict": j.verdict,
                         "model_verdict": j.model_verdict, "problems": list(j.problems)})
        if stopped:
            break
    decision = decide(cases, rows)
    record = {"prompt": SUPPORT_PROMPT_VERSION, "model": model.model, "repeats": repeats,
              "allocation_usd": cap, "spent_estimate_usd": round(budget.spent, 6),
              "calls": budget.calls, "stopped": stopped, "decision": decision, "rows": rows,
              "provider_log": budget.log}
    (out / "research-integrity-gate.json").write_text(json.dumps(record, indent=2),
                                                     encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("spent_estimate_usd", "calls", "stopped")}))
    print(json.dumps(decision, indent=1)[:4000])
    assert decision["status"] == "PASS", decision
