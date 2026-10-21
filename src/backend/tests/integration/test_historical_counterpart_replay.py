"""T18 CS1A replay on the saved T18 evaluation, offline: the production `screen` and
`counterpart` with the real local bge embedder, on the stored Claims, cited Passages and
re-fetched Observations of the 11 Claims T18 left unverified. No model is called: this proves
which Claims reach the entailment call, never what the judge would say.

The T18 evaluation database holds third-party web text, so it is not committed; set
T18_EVAL_DB to its path (T18-evidence-package/raw/eval.db) to run this, else it is skipped."""

import json
import os
from datetime import date
import sqlite3
from pathlib import Path

import pytest

from insightforge_agent.domain.contracts import UNVERIFIED_MARK
from insightforge_agent.domain.memory import without_report_date
from insightforge_agent.domain.models import Passage
from insightforge_agent.embeddings import FastEmbedEmbedder
from insightforge_agent.pipeline.fact_check import counterpart, screen

pytestmark = pytest.mark.integration

DB = os.environ.get("T18_EVAL_DB")
# The one Claim whose figures are in its counterpart but further apart than a 4-sentence span.
FIGURES_TOO_FAR_APART = ("run_fc1c9560577b", 2, 1)


@pytest.fixture(scope="module")
def t18():
    if not DB or not Path(DB).is_file():
        pytest.skip("T18_EVAL_DB is not set to the saved T18 eval.db")
    con = sqlite3.connect(f"{Path(DB).resolve().as_uri()}?mode=ro", uri=True)
    yield con
    con.close()


def passages(con, observation_id):
    return [Passage(observation_id=observation_id, index=i, text=t)
            for i, t in con.execute("SELECT idx, text FROM passages WHERE observation_id=? "
                                    "ORDER BY idx", (observation_id,))]


def test_the_eleven_t18_unverified_claims_reach_entailment_only_through_the_counterpart(
        t18, make_deps):
    deps = make_deps(embedder=FastEmbedEmbedder("BAAI/bge-small-en-v1.5"))
    seen = {}
    for run_id, body in t18.execute("SELECT run_id, body FROM reports"):
        report = json.loads(body)
        for v in report["verified"]["verdicts"]:
            if v["verdict"] != "unverified":
                continue
            claim = report["draft"]["sections"][v["section"]]["claims"][v["claim"]]["text"]
            claim = claim.replace(UNVERIFIED_MARK, "").strip()
            cited = next(p for p in passages(t18, v["original"]["observation_id"])
                         if p.index == v["original"]["index"])
            refetched = passages(t18, v["matched"]["observation_id"])
            f2 = screen(deps, claim, refetched)
            found, match = counterpart(cited, refetched)
            fallback = screen(deps, claim, [found], floor=-1.0) if found else None
            seen[(run_id, v["section"], v["claim"])] = (f2, match, fallback)
    assert len(seen) == 11
    for key, (f2, match, fallback) in seen.items():
        assert f2.candidates == [], key  # F2 had nothing: this is where the fallback applies
        assert match == "unique", key  # the cited text is on the re-fetched page, once
        assert bool(fallback.candidates) is (key != FIGURES_TOO_FAR_APART), key


def test_the_twenty_seven_t18_verified_claims_never_reach_the_fallback(t18, make_deps):
    """R7: every Claim T18 verified had an eligible F2 candidate, so the fallback, which runs
    only when F2 has none, cannot change them."""
    deps = make_deps(embedder=FastEmbedEmbedder("BAAI/bge-small-en-v1.5"))
    checked = 0
    for _, body in t18.execute("SELECT run_id, body FROM reports"):
        report = json.loads(body)
        for v in report["verified"]["verdicts"]:
            if v["verdict"] != "verified":
                continue
            claim = report["draft"]["sections"][v["section"]]["claims"][v["claim"]]["text"]
            claim = claim.replace(UNVERIFIED_MARK, "").strip()
            if v["check"].startswith("historical_"):  # checked without its report date
                item = next(i for i in report["historical"]["items"]
                            if i["primary"] == v["original"])
                claim = without_report_date(claim, date.fromisoformat(item["report_date"]))
            if v["check"].endswith("document_passages"):
                continue  # documents keep their own check
            refetched = passages(t18, v["matched"]["observation_id"])
            assert screen(deps, claim, refetched).candidates, (v["check"], claim)
            checked += 1
    assert checked >= 20
