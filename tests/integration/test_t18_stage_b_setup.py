"""T18 Stage B, offline: the paid harness (tests/live/test_t18_stage_b.py) is set up as the spec
says, before anyone authorises it. No model is called. The case-file checks always run; the
checks against the saved T18 data need T18_EVAL_DB (integration)."""

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import batch_passages, split_by_passages

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "t18"
CASES = json.loads((FIXTURES / "stage_b1_cases.json").read_text(encoding="utf-8"))
DB = os.environ.get("T18_EVAL_DB")


@pytest.fixture(scope="module")
def t18():
    if not DB or not Path(DB).is_file():
        pytest.skip("T18_EVAL_DB is not set to the saved T18 eval.db")
    con = sqlite3.connect(f"{Path(DB).resolve().as_uri()}?mode=ro", uri=True)
    yield con
    con.close()


@pytest.mark.integration
def test_76_of_the_77_b1_assessments_reach_the_judge_and_n31_does_not(t18, make_deps):
    """B-1 assesses 77 cases: 10 positives, 40 negatives and T18's 27 verified Claims. Only
    N31 is blocked by the figure gate, so a run makes 76 calls. The two Historical Claims reach
    the judge because they are checked without their report-date annotation, as production
    checks them (B1-03); the first B-1 run sent them annotated and they never did."""
    from insightforge_agent.embeddings import FastEmbedEmbedder
    from insightforge_agent.pipeline.fact_check import counterpart, screen
    from tests.t18_stage_b_support import passages, verdicts

    deps = make_deps(embedder=FastEmbedEmbedder("BAAI/bge-small-en-v1.5"))
    stored = verdicts(t18)
    reach = Counter()
    for g in CASES["groups"]:
        v, _ = stored[g["ref"]]
        cited = next(p for p in passages(t18, v["original"]["observation_id"])
                     if p.index == v["original"]["index"])
        found, _ = counterpart(cited, passages(t18, v["matched"]["observation_id"]))
        for case in [{"id": "P", "text": g["positive"]}] + g["negatives"]:
            ok = bool(screen(deps, case["text"], [found], floor=-1.0).candidates)
            reach[(case["id"] == "P", ok)] += 1
            if case["id"] == "N31":
                assert not ok
    assert reach == {(True, True): 10, (False, True): 39, (False, False): 1}
    verified = {k: claim for k, (v, claim) in stored.items() if v["verdict"] == "verified"}
    assert len(verified) == 27
    for key, claim in verified.items():  # as the harness screens them
        v, _ = stored[key]
        assert screen(deps, claim, passages(t18, v["matched"]["observation_id"])).candidates, key
    for key in ("run_7be9b5b129d0 2.0", "run_7be9b5b129d0 3.1"):
        assert "(reported" not in verified[key]
        assert verified[key].startswith("The earlier report found that")  # framing kept


@pytest.mark.integration
def test_the_b2_batches_rebuild_the_twelve_t18_drafting_calls(t18):
    spec = json.loads((FIXTURES / "stage_b2_batches.json").read_text(encoding="utf-8"))
    refs = list(dict.fromkeys(tuple(r) for b in spec["batches"] for r in b))
    texts = {}
    for obs, idx in refs:
        texts[(obs, idx)] = t18.execute("SELECT text FROM passages WHERE observation_id=? AND "
                                        "idx=?", (obs, idx)).fetchone()[0]
    ordered = [Passage(observation_id=o, index=i, text=texts[(o, i)]) for o, i in refs]
    batches = batch_passages(ordered, 3000)
    assert len(batches) == 12  # as T18 drafted them (7 failed)
    calls = {cap: sum(len(split_by_passages(b, cap)) for b in batches) for cap in (150, 120)}
    assert 12 < calls[150] <= calls[120]  # a smaller cap never means fewer calls
