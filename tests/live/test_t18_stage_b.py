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
configuration's results and the cap decision (`b2_cap_decision`) are recorded. B-2 drafts
only through Anthropic native structured output, built on the chat model itself
(`drafting_runner` refuses otherwise, and the record must say `native_structured_output`).
A missing T18 database fails the run; it never skips as if it had passed."""

import json
from pathlib import Path

import pytest

from tests.t18_acceptance import b1_failures, b2_cap_decision, b2_failures, b2_summary
from tests.t18_stage_b_support import (
    checker_deps,
    checker_model,
    drafting_runner,
    judged,
    passages,
    require_t18_db,
    run_b2,
    verdicts,
    write,
)

pytestmark = pytest.mark.live
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "t18"


def test_b1_entailment_calibration():
    from insightforge_agent.pipeline.fact_check import counterpart, screen

    con = require_t18_db()
    cases = json.loads((FIXTURES / "stage_b1_cases.json").read_text(encoding="utf-8"))
    bound, budget = checker_model("b1")
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
    con = require_t18_db()
    spec = json.loads((FIXTURES / "stage_b2_batches.json").read_text(encoding="utf-8"))
    texts = {}
    for obs in {o for b in spec["batches"] for o, _ in b}:
        texts.update({(obs, p.index): p.text for p in passages(con, obs)})
    model, native, budget = drafting_runner("b2")  # raises unless native output is active
    record = run_b2(texts, spec, model, native, budget)
    assert record["native_structured_output"] is True
    record["summary"], record["cap_decision"] = b2_summary(record), b2_cap_decision(record)
    write("b2", record)
    failures = b2_failures(record)
    assert not failures, "\n".join(failures)
