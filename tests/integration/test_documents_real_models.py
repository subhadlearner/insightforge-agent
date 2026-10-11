"""T7 (#8) with the real libraries: Docling chunks a PDF, bge-small embeds it, Qdrant searches it,
and the full pipeline (scripted chat models) cites and fact-checks document Claims."""

from pathlib import Path

from qdrant_client import QdrantClient

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.stores.doc_chunks import COLLECTION, DocChunkIndex
from tests.pipeline.test_t7_documents import events, researchers
from tests.scripted import ScriptedChat, ai, call

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "samples" / "documents" / "northwind-health-annual-report-2025.pdf"
MODEL = "BAAI/bge-small-en-v1.5"


def upload() -> Upload:
    return Upload(filename=SAMPLE.name, data=SAMPLE.read_bytes())


def test_docling_chunks_by_paragraph_with_page_and_section_heading(chunker):
    drafts = chunker.chunk(SAMPLE.name, SAMPLE.read_bytes())
    by_text = {d.text: d for d in drafts}
    revenue = next(d for t, d in by_text.items() if "612 million dollars" in t)
    assert (revenue.page, revenue.section_heading) == (1, "Letter to shareholders")
    rnd = next(d for t, d in by_text.items() if "71 million dollars" in t)
    assert (rnd.page, rnd.section_heading) == (2, "Research and development")
    risk = next(d for t, d in by_text.items() if "titanium" in t)
    assert (risk.page, risk.section_heading) == (3, "Principal risks")


def test_semantic_search_finds_a_paraphrased_question_with_the_real_embedder(make_deps, chunker,
                                                                             embedder):
    index = DocChunkIndex(QdrantClient(":memory:"), embedder, MODEL, embedder.dimensions)
    deps = make_deps(chunker=chunker, doc_index=index)
    from insightforge_agent.pipeline.ingest import ingest_documents

    ingest_documents(deps, "alice", "run_1", [upload()])
    # Few words in common with the chunk: "R&D budget" vs "Research and development spending".
    hits = index.search("alice", "run_1", "How large was the R&D budget last year?")
    top = deps.store.get_passages("alice", hits[0].passage.observation_id, [hits[0].passage.index])[0]
    assert "Research and development spending" in top.text and top.page == 2
    hits = index.search("alice", "run_1", "Which supply chain dependency could hurt the business?")
    texts = [deps.store.get_passages("alice", h.passage.observation_id, [h.passage.index])[0].text
             for h in hits[:3]]
    assert any("titanium" in t for t in texts)
    assert len(index.search("alice", "run_1", "anything")) <= 8
    assert index.search("bob", "run_1", "R&D budget") == []
    info = index._client.get_collection(COLLECTION)
    assert info.config.params.vectors.size == 384
    (point,), _ = index._client.scroll(COLLECTION, limit=1)
    assert point.payload["embedding_model"] == MODEL


def test_the_pipeline_cites_and_fact_checks_the_real_pdf(make_deps, chunker, embedder):
    queries = {"s1": "Northwind Health revenue fiscal 2025",
               "s2": "research and development pipeline NW-200",
               "s3": "principal risks supplier pricing"}
    plan = {"plan": {
        "aspects": [{"id": f"a{n}", "name": f"Aspect {n}"} for n in (1, 2, 3)],
        "sub_tasks": [{"id": s, "aspect_id": f"a{n}", "query": q, "source_type": "documents",
                       "time_horizon_months": 12, "priority": n}
                      for n, (s, q) in enumerate(queries.items(), start=1)]}}
    todos = call("write_todos", {"todos": [{"content": q, "status": "pending"}
                                           for q in queries.values()]}, "todo-1")
    tasks = [call("task", {"description": f"SUBTASK_ID={s} {q}", "subagent_type": "doc_reader"},
                  f"research-{s}") for s, q in queries.items()]
    planner = ScriptedChat(script=[ai(call("submit_plan", plan, "plan-1")), ai(text="ok"),
                                   ai(todos, *tasks), ai(text="Dispatched.")])
    index = DocChunkIndex(QdrantClient(":memory:"), embedder, MODEL, embedder.dimensions)
    deps = make_deps(planner_model=planner, researcher_model=researchers(), chunker=chunker,
                     doc_index=index, embedder=embedder)
    u = upload()
    run = run_brief(deps, "alice", Brief(text="Assess Northwind Health from its annual report.",
                                         source_toggles={"web": False, "documents": True},
                                         document_ids=[u.document_id]), uploads=[u])
    assert run.state == RunState.COMPLETE, [e.payload for e in events(deps, run, "run_failed")]
    report = deps.repos.reports.get_for_run("alice", run.id)
    citations = report_citations(deps.store, "alice", report)
    assert citations and all(c.source_title == SAMPLE.name and c.page for c in citations)
    assert all(c.section_heading for c in citations)
    verdicts = report.body["verified"]["verdicts"]
    assert verdicts and all(v["check"] == "document_passages" for v in verdicts)
    # The fake Writer restates each Evidence item verbatim, so a correct check verifies them.
    assert all(v["verdict"] == "verified" for v in verdicts)
