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


@pytest.mark.integration
def test_with_same_passage_context_the_judge_sees_the_antecedents_and_nothing_more(t18, make_deps):
    """B-1 run 2 review: what the judge is shown under the same-Passage context rule, on the
    saved data. 2.6 and 3.1 regain the subject and place their own Passage names before the
    span; 3.3 gains nothing (its solid-state topic is only in the Source title, never shown);
    N25 and N40 are shown nothing that names the IEA report or an exemption. Whether the model
    then judges them correctly is for an authorised B-1 run, not this test."""
    from insightforge_agent.domain.tokens import estimate_tokens
    from insightforge_agent.embeddings import FastEmbedEmbedder
    from insightforge_agent.pipeline.fact_check import _shown, counterpart, screen
    from insightforge_agent.pipeline.support import render_request
    from tests.t18_stage_b_support import passages, verdicts

    deps = make_deps(embedder=FastEmbedEmbedder("BAAI/bge-small-en-v1.5"))
    stored = verdicts(t18)

    def shown(claim, candidates):
        # ADR-0006: the support request the Fact-Checker sends, sized on its user message.
        request = render_request(claim, [c.unit for c in _shown(candidates, None)])
        assert estimate_tokens(request) <= 3000
        return request.split("\n\n", 1)[1]  # the Passage part only, not the statement

    def verified(key):
        v, claim = stored[key]
        return shown(claim, screen(deps, claim, passages(t18, v["matched"]["observation_id"]))
                     .candidates)

    def whole(key):  # the matched Passage, complete, as the judge's one numbered item
        v, _ = stored[key]
        text = next(p.text for p in passages(t18, v["matched"]["observation_id"])
                    if p.index == v["matched"]["index"])
        return "[1] " + " ".join(text.split())

    for key, antecedent in (("run_06105d324454 2.6", "the LFP battery industry faces"),
                            ("run_7be9b5b129d0 3.1", "China dominated the regional market")):
        assert verified(key) == whole(key) and antecedent in verified(key), key
    # 3.3: its own complete Passage, which never says "solid-state" (adjudicated INSUFFICIENT)
    assert verified("run_06105d324454 3.3") == whole("run_06105d324454 3.3") == (
        "[1] BYD: Targeting 400 Wh/kg with 5C charging, initial production 2027")
    assert "solid" not in verified("run_06105d324454 3.3").casefold()

    negatives = {n["id"]: (g["ref"], n["text"]) for g in CASES["groups"] for n in g["negatives"]}
    for case, absent in (("N25", ("IEA", "World Energy Outlook")),
                         ("N40", ("exempt", "licens", "all export"))):
        ref, text = negatives[case]
        v, _ = stored[ref]
        cited = next(p for p in passages(t18, v["original"]["observation_id"])
                     if p.index == v["original"]["index"])
        found, _ = counterpart(cited, passages(t18, v["matched"]["observation_id"]))
        seen = shown(text, screen(deps, text, [found], floor=-1.0).candidates)
        assert not any(a.casefold() in seen.casefold() for a in absent), case
