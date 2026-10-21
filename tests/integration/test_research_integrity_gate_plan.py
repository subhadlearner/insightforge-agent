"""The ADR-0006 live support gate, built offline from the saved T18 database: every case resolves
its cited Passage (no paid call is ever made here), and each request holds exactly that Passage,
within the per-call cap. This is the dry run that must pass before the paid gate is authorised.

The T18 evaluation database holds third-party web text, so it is not committed; set T18_EVAL_DB
to its path (the T18 baseline eval.db, sha256 d061f8b1...9aff) to run this."""

import os
import sqlite3
from pathlib import Path

import pytest

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.pipeline.support import Unit, render_request, request_tokens
from tests.research_integrity_gate import b1_cases, decide, gold_cases, holdout_cases

pytestmark = pytest.mark.integration
DB = os.environ.get("T18_EVAL_DB")


@pytest.fixture(scope="module")
def con():
    if not DB or not Path(DB).is_file():
        pytest.skip("T18_EVAL_DB is not set to the saved T18 eval.db")
    c = sqlite3.connect(f"{Path(DB).resolve().as_uri()}?mode=ro", uri=True)
    yield c
    c.close()


def test_every_gate_case_resolves_its_cited_passage(con):
    cases = gold_cases(repeats=3) + b1_cases(con, repeats=3)
    counts = {s: sum(1 for c in cases if c.set == s) for s in
              ("gold", "b1_positive", "b1_negative", "b1_verified")}
    assert counts == {"gold": 20, "b1_positive": 10, "b1_negative": 40, "b1_verified": 27}
    verified = {c.id: c.expected for c in cases if c.set == "b1_verified"}
    assert sum(e == "NOT_SUPPORTED" for e in verified.values()) == 3
    assert {k for k, e in verified.items() if e == "NOT_SUPPORTED"} == {
        "run_ab1c836b61f4 1.5", "run_ab1c836b61f4 2.9", "run_06105d324454 3.3"}


def test_each_request_holds_only_the_cited_unit_and_fits_the_cap(con):
    for c in gold_cases(repeats=1) + b1_cases(con, repeats=1) + holdout_cases(repeats=1):
        unit = Unit(PassageRef(observation_id=c.ref[0], index=c.ref[1]), c.passage, c.entity)
        request = render_request(c.statement, [unit])
        assert request.count("\n[1] ") == 1 and "[2]" not in request.split("\n[1] ", 1)[0]
        assert request_tokens(c.statement, [unit]) <= 3000, c.id
        if c.entity is None:
            assert "Identity context" not in request, c.id


def test_the_planned_judgment_count_and_a_dry_decision(con):
    cases = gold_cases(repeats=3) + b1_cases(con, repeats=3) + holdout_cases(repeats=3)
    judgments = sum(c.repeats for c in cases if c.expected != "REPORT") + sum(
        1 for c in cases if c.expected == "REPORT")
    assert judgments == 159 + 24  # the first attempt's 159, and the held-out set's 6 + 6 x 3
    assert decide(cases, [])["status"] == "INCONCLUSIVE"  # nothing judged is never a pass
