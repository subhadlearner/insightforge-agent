"""T18 CS1B replay on the saved T18 evaluation, offline: the production `literal_problem` on
every Evidence item T18 kept, with each Run's own lower-case vocabulary. Pins the v3.1 finding:
the revised name rule newly rejects exactly one kept item, whose cited Passage never says "US".

The T18 evaluation database holds third-party web text, so it is not committed; set
T18_EVAL_DB to its path (T18-evidence-package/raw/eval.db) to run this, else it is skipped."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from insightforge_agent.domain.provenance import common_words
from insightforge_agent.pipeline.synthesize import literal_problem

pytestmark = pytest.mark.integration
DB = os.environ.get("T18_EVAL_DB")


@pytest.fixture(scope="module")
def t18():
    if not DB or not Path(DB).is_file():
        pytest.skip("T18_EVAL_DB is not set to the saved T18 eval.db")
    con = sqlite3.connect(f"{Path(DB).resolve().as_uri()}?mode=ro", uri=True)
    yield con
    con.close()


def test_the_revised_name_rule_keeps_what_t18_kept_but_one(t18):
    newly_rejected, kept = [], 0
    for run_id, body in t18.execute("SELECT run_id, body FROM reports"):
        refetched = {json.loads(p)["observation_id"] for (p,) in t18.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND type='fact_check_refetched'",
            (run_id,))}
        common = common_words(t for o, t in t18.execute(
            "SELECT p.observation_id, p.text FROM passages p JOIN observations o "
            "ON o.id = p.observation_id WHERE o.run_id=?", (run_id,)) if o not in refetched)
        for section in json.loads(body)["evidence"]["sections"]:
            for item in section["items"]:
                kept += 1
                texts = [t18.execute("SELECT text FROM passages WHERE observation_id=? AND idx=?",
                                     (r["observation_id"], r["index"])).fetchone()[0]
                         for r in item["supporting"]]
                problems = [literal_problem(statement=item["statement"], entities=[],
                                            entity=item["entity"], passage_text=t, common=common)
                            for t in texts]
                if all(problems):
                    newly_rejected.append((item["statement"], problems[0]))
    assert kept == 333
    assert [(s[:24], p) for s, p in newly_rejected] == [
        ("US commercial solar is e", "name not in Passage: ['US']")]
