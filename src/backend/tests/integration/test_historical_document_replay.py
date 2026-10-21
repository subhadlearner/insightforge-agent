"""T18 CS2 replay on the saved T18 document Run (run_0b45a66fd66d), offline, with production
code: s2's own stored searches are recoverable under the recorded failure reply and bring back
the R&D chunks; the six Northwind segment and risk statements T18 dropped now pass the literal
check on their real chunks, with the document entity the PDF itself establishes.

The T18 evaluation database holds the uploaded test PDF's chunks and third-party web text, so it
is not committed; set T18_EVAL_DB to its path (T18-evidence-package/raw/eval.db) to run this."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from insightforge_agent.domain.document_recovery import recoverable, recovered_chunks
from insightforge_agent.domain.provenance import common_words, document_entity
from insightforge_agent.pipeline.synthesize import literal_problem

pytestmark = pytest.mark.integration
DB = os.environ.get("T18_EVAL_DB")
RUN, OBS = "run_0b45a66fd66d", "obs_5e2476030808cf39"
# s2's Document Reader reply, from the T18 Planner thread (model output).
S2_REPLY = ("FAILED: doc_search returned only metadata (source file, page, and relevance score) "
            "for 8 chunks from northwind-health-annual-report-2025.pdf, pages 1-3, across two "
            "queries, with no text content. I can't confirm that any of them cover the pipeline, "
            "product stages, or R&D spending, so I'm not listing them as supporting chunks. The "
            "likely candidates are obs_5e2476030808cf39:0, :10, and :7, but they need to be read "
            "directly to verify.")


@pytest.fixture(scope="module")
def t18():
    if not DB or not Path(DB).is_file():
        pytest.skip("T18_EVAL_DB is not set to the saved T18 eval.db")
    con = sqlite3.connect(f"{Path(DB).resolve().as_uri()}?mode=ro", uri=True)
    yield con
    con.close()


def chunks(con) -> dict[int, str]:
    return dict(con.execute("SELECT idx, text FROM passages WHERE observation_id=?", (OBS,)))


def test_s2_is_recovered_with_the_rnd_chunks(t18):
    searches = [json.loads(p)["chunk_ids"] for (p,) in t18.execute(
        "SELECT payload FROM run_events WHERE run_id=? AND type='doc_search' ORDER BY seq",
        (RUN,)) if json.loads(p)["subtask_id"] == "s2"]
    ok, _ = recoverable(S2_REPLY, searches)
    chosen = recovered_chunks(searches)
    assert ok and len(searches) == 2
    assert {f"{OBS}:{i}" for i in (3, 4, 5)} <= set(chosen)  # R&D spend, programmes, NW-200 trial
    text = chunks(t18)
    assert "71 million dollars" in text[3] and "6 programmes" in text[4]


def test_the_six_northwind_statements_t18_dropped_now_pass_the_literal_check(t18):
    text = chunks(t18)
    title = t18.execute("SELECT s.title FROM observations o JOIN sources s ON s.id=o.source_id "
                        "WHERE o.id=?", (OBS,)).fetchone()[0]
    entity = document_entity(title, [text[i] for i in sorted(text)])
    assert entity == "Northwind Health"
    common = common_words(t for (t,) in t18.execute(
        "SELECT p.text FROM passages p JOIN observations o ON o.id=p.observation_id "
        "WHERE o.run_id=?", (RUN,)))
    dropped = [json.loads(p) for (p,) in t18.execute(
        "SELECT payload FROM run_events WHERE run_id=? AND type='evidence_dropped' ORDER BY seq",
        (RUN,))]
    northwind = [d["statement"] for d in dropped
                 if any("Northwind Health" in r for r in d.get("rejected", []))]
    assert len(northwind) == 6
    for statement in northwind:
        chunk = 9 if statement.startswith("Northwind Health discloses") else (
            1 if "341" in statement or "187" in statement else 2)
        assert literal_problem(statement=statement, entities=["Northwind Health"],
                               entity="Northwind Health", passage_text=text[chunk],
                               common=common, document_entity=entity) is None, statement
