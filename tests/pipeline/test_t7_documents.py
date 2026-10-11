"""T7 (#8): uploaded documents end to end, with scripted models, a scripted chunker and
in-memory Qdrant. No network, no Docling models, no paid calls.

Covers: INGESTING runs only with uploads; Doc Reader isolation (chunk IDs only, k = 8, own
Run and owner only); Extraction and Synthesis read only the retrieved chunks; document
citations (page, section heading); the Fact-Checker's document path; chunk retention across
Runs; and the failure paths."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from qdrant_client import QdrantClient

from insightforge_agent.agents.doc_reader import ChunkingError
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.models import RunState
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, ai, call

BRIEF = "Assess Orrin Bio from its annual report: revenue, research pipeline and market."
REPORT = [  # (page, section heading, chunk text); one sentence each, as the fake models expect
    (1, "Letter to shareholders", "Orrin Bio revenue reached 412 million dollars in fiscal 2025."),
    (1, "Letter to shareholders", "Orrin Bio revenue grew 18 percent compared with fiscal 2024."),
    (2, "Research pipeline", "The research pipeline holds 9 clinical programmes in 2025."),
    (2, "Research pipeline", "Two research programmes entered phase 3 trials in 2025."),
    (3, "Operations", "The company opened a manufacturing site in Ireland."),
    (3, "Operations", "Headcount reached 1,250 employees at year end."),
    (4, "Governance", "The board met eleven times during the year."),
    (4, "Governance", "Auditors issued an unqualified opinion."),
    (5, "Sustainability", "Scope 1 emissions fell 12 percent."),
    (5, "Sustainability", "Water use per batch declined."),
    (6, "Shareholder information", "The annual general meeting is held in Boston."),
    (6, "Shareholder information", "Dividends were not paid in the year."),
] + [(7 + n // 4, "Appendix", f"Appendix note {n} lists office leases in city {n}.")
     for n in range(12)]  # 24 chunks: two searches of k = 8 cannot return them all
DOC_QUERIES = {"s1": "Orrin Bio revenue fiscal 2025 million dollars",
               "s2": "research pipeline clinical programmes phase 3 trials 2025"}
WEB_QUERY = fakes.QUERIES["s3"]
PLAN = {"plan": {
    "aspects": [{"id": "a1", "name": "Revenue"}, {"id": "a2", "name": "Pipeline"},
                {"id": "a3", "name": "Market"}],
    "sub_tasks": [
        {"id": "s1", "aspect_id": "a1", "query": DOC_QUERIES["s1"], "source_type": "documents",
         "time_horizon_months": 12, "priority": 1},
        {"id": "s2", "aspect_id": "a2", "query": DOC_QUERIES["s2"], "source_type": "documents",
         "time_horizon_months": 12, "priority": 2},
        {"id": "s3", "aspect_id": "a3", "query": WEB_QUERY, "source_type": "web",
         "time_horizon_months": 12, "priority": 3},
    ]}}
AGENT = {"s1": "doc_reader", "s2": "doc_reader", "s3": "researcher"}
QUERY = {**DOC_QUERIES, "s3": WEB_QUERY}


def pdf(name: str = "orrin-annual-2025.pdf", tag: bytes = b"") -> Upload:
    return Upload(filename=name, data=b"%PDF-1.7 orrin " + name.encode() + tag)


class Chunker:
    """Stands in for Docling: every upload is the annual report, unless named broken*.pdf."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def chunk(self, filename: str, data: bytes) -> list[PassageDraft]:
        self.calls.append(filename)
        if filename.startswith("broken"):
            raise ChunkingError(f"{filename}: conversion failure")
        return [PassageDraft(t, page=p, section_heading=h) for p, h, t in REPORT]


def planner(agents=AGENT) -> ScriptedChat:
    todos = call("write_todos", {"todos": [{"content": q, "status": "pending"}
                                           for q in QUERY.values()]}, "todo-1")
    tasks = [call("task", {"description": f"SUBTASK_ID={sid} {QUERY[sid]}",
                           "subagent_type": agents[sid]}, f"research-{sid}")
             for sid in QUERY]
    return ScriptedChat(script=[ai(call("submit_plan", PLAN, "plan-1")), ai(text="Plan submitted."),
                                ai(todos, *tasks), ai(text="Dispatched.")])


def researchers(extra_chunk_ids: tuple[str, ...] = ()) -> ScriptedChat:
    """One model for both subagents, told apart by their system prompt. The Doc Reader names
    every chunk its search returned, plus `extra_chunk_ids` (to test invented IDs)."""
    def reply(messages) -> AIMessage:
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        last = messages[-1]
        reader = "Document Reader" in system
        if isinstance(last, ToolMessage):
            hits = json.loads(str(last.content))
            if reader:
                ids = [h["chunk_id"] for h in hits] + list(extra_chunk_ids)
                return AIMessage(content=f"RESULT: relevant chunks | chunks: {', '.join(ids)}")
            return AIMessage(content="RESULT: found | sources: "
                             + ", ".join(h["source_id"] for h in hits))
        text = "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
        query = text.split(" ", 1)[1].strip() if "SUBTASK_ID=" in text else text
        tool = "doc_search" if reader else "web_search"
        return ai(call(tool, {"query": query}, f"{tool}-{abs(hash(text))}"))
    return ScriptedChat(reply=reply)


@pytest.fixture
def doc_deps(make_deps):
    def make(**overrides):
        parts = dict(planner_model=planner(), researcher_model=researchers(), chunker=Chunker(),
                     doc_index=DocChunkIndex(QdrantClient(":memory:"), HashingEmbedder(),
                                             "test-hash", 256))
        parts.update(overrides)
        return make_deps(**parts)
    return make


def brief_with(uploads, web=True) -> Brief:
    return Brief(text=BRIEF, source_toggles={"web": web, "documents": True},
                 document_ids=[u.document_id for u in uploads])


def events(deps, run, type_=None):
    out, cursor = [], 0
    while batch := deps.repos.events.read_after(run.owner_id, run.id, cursor):
        out += batch
        cursor = batch[-1].seq
    return [e for e in out if type_ is None or e.type == type_]


def run_with_document(deps, owner="alice", uploads=None, web=True):
    uploads = uploads or [pdf()]
    return run_brief(deps, owner, brief_with(uploads, web), uploads=uploads)


# --- INGESTING ----------------------------------------------------------------------------

def test_a_run_with_an_upload_ingests_it_first_and_completes(doc_deps):
    deps = doc_deps()
    run = run_with_document(deps)
    assert run.state == RunState.COMPLETE, [e.payload for e in events(deps, run, "run_failed")]
    stages = [e.payload["state"] for e in events(deps, run, "stage")]
    assert stages[:2] == ["INGESTING", "PLANNING"] and stages[-1] == "COMPLETE"
    (ingested,) = [e.payload for e in events(deps, run, "document_ingested")]
    assert ingested["filename"] == "orrin-annual-2025.pdf" and ingested["chunks"] == len(REPORT)
    assert ingested["pages"] == 9
    source = deps.repos.sources.get("alice", ingested["source_id"])
    assert source.kind == "pdf" and source.title == "orrin-annual-2025.pdf"
    passages = deps.repos.passages.list("alice", ingested["observation_id"])
    assert [(p.page, p.section_heading, p.text) for p in passages] == REPORT


def test_ingesting_is_skipped_without_uploads(doc_deps):
    deps = doc_deps(planner_model=fakes.planner(), researcher_model=fakes.researcher())
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert "INGESTING" not in [e.payload["state"] for e in events(deps, run, "stage")]
    assert deps.chunker.calls == []


def test_the_same_file_uploaded_twice_is_ingested_once(doc_deps):
    deps = doc_deps()
    a, b = pdf("a.pdf"), Upload(filename="copy.pdf", data=pdf("a.pdf").data)
    run = run_brief(deps, "alice", brief_with([a, b]), uploads=[a, b])
    assert run.state == RunState.COMPLETE
    assert len(events(deps, run, "document_ingested")) == 1


# --- Doc Reader isolation -----------------------------------------------------------------

def doc_reader_conversations(model):
    return [conv for conv in model.seen
            if any("Document Reader" in str(m.content) for m in conv if isinstance(m, SystemMessage))]


def test_the_doc_reader_receives_only_chunk_ids_never_text(doc_deps):
    model = researchers()
    deps = doc_deps(researcher_model=model)
    run_with_document(deps)
    convs = doc_reader_conversations(model)
    assert len(convs) >= 2  # s1 and s2
    for conv in convs:
        seen = "\n".join(str(m.content) for m in conv)
        assert not any(text in seen for _, _, text in REPORT)
        assert BRIEF not in seen
        for m in conv:
            if isinstance(m, ToolMessage):
                hits = json.loads(str(m.content))
                assert 0 < len(hits) <= 8
                assert all(set(h) == {"chunk_id", "source_id", "source_filename", "page",
                                      "relevance_score"} for h in hits)


def test_a_documents_subtask_sent_to_the_web_researcher_is_refused(doc_deps):
    deps = doc_deps(planner_model=planner({**AGENT, "s1": "researcher"}))
    run = run_with_document(deps)
    refused = [e.payload for e in events(deps, run, "dispatch_rejected")]
    assert refused and refused[0]["subtask_id"] == "s1"
    assert "doc_reader" in refused[0]["reason"]


def test_an_invented_chunk_id_is_ignored(doc_deps):
    deps = doc_deps(researcher_model=researchers(extra_chunk_ids=("obs_0123456789abcdef:3",)))
    run = run_with_document(deps)
    assert run.state == RunState.COMPLETE
    report = deps.repos.reports.get_for_run("alice", run.id)
    cited = {i["primary"]["observation_id"] for s in report.body["evidence"]["sections"]
             for i in s["items"]}
    assert "obs_0123456789abcdef" not in cited


# --- only retrieved chunks are read ---------------------------------------------------------

def test_extraction_and_synthesis_read_only_the_retrieved_chunks(doc_deps):
    light = fakes.light()
    deps = doc_deps(light_model=light)
    run = run_with_document(deps)
    assert run.state == RunState.COMPLETE
    left_out = not_retrieved(deps, run)
    assert left_out
    reads = "\n".join(str(conv[-1].content) for conv in light.seen)
    assert "Orrin Bio revenue reached 412 million dollars" in reads
    assert not any(text in reads for text in left_out)


def not_retrieved(deps, run) -> list[str]:
    """Texts of the uploaded chunks no Doc Reader search returned in this Run."""
    returned = {c for e in events(deps, run, "doc_search") for c in e.payload["chunk_ids"]}
    (ingested,) = [e.payload for e in events(deps, run, "document_ingested")]
    passages = deps.repos.passages.list(run.owner_id, ingested["observation_id"])
    return [p.text for p in passages if f"{p.observation_id}:{p.index}" not in returned]


def test_graph_state_holds_no_upload_bytes_or_chunk_text(doc_deps):
    deps = doc_deps()
    snapshots = []
    run = run_brief(deps, "alice", brief_with([pdf()]), uploads=[pdf()],
                    on_state=snapshots.append)
    dumped = json.dumps(snapshots, default=str)
    assert "%PDF" not in dumped  # the file name may appear, as a Source title
    # Evidence statements may be in state (short sentences); unread chunks never are.
    assert not any(text in dumped for text in not_retrieved(deps, run))


# --- citations and fact-checking ------------------------------------------------------------

def test_document_claims_cite_the_chunk_with_its_page_and_heading(doc_deps):
    deps = doc_deps()
    run = run_with_document(deps)
    report = deps.repos.reports.get_for_run("alice", run.id)
    doc_citations = [c for c in report_citations(deps.store, "alice", report)
                     if c.source_title == "orrin-annual-2025.pdf"]
    assert doc_citations
    for c in doc_citations:
        page, heading = next((p, h) for p, h, t in REPORT if t == c.passage_text)
        assert (c.page, c.section_heading) == (page, heading)


def test_document_claims_are_fact_checked_against_the_stored_chunks(doc_deps):
    deps = doc_deps()
    run = run_with_document(deps)
    report = deps.repos.reports.get_for_run("alice", run.id)
    evidence = {i["id"]: i for s in report.body["evidence"]["sections"] for i in s["items"]}
    pdf_obs = {e.payload["observation_id"] for e in events(deps, run, "document_ingested")}
    verdicts = report.body["verified"]["verdicts"]
    doc_verdicts = [v for v in verdicts
                    if evidence[v["evidence_id"]]["primary"]["observation_id"] in pdf_obs]
    assert doc_verdicts, "the sample should include a document Claim"
    assert all(v["check"] == "document_passages" for v in doc_verdicts)
    assert all(v["verdict"] == "verified" for v in doc_verdicts)
    assert all(v["matched"]["observation_id"] in pdf_obs for v in doc_verdicts)


# --- isolation and retention ----------------------------------------------------------------

def test_a_later_run_cannot_search_earlier_uploads_but_earlier_citations_still_resolve(doc_deps):
    deps = doc_deps()
    first = run_with_document(deps)
    report = deps.repos.reports.get_for_run("alice", first.id)
    before = report_citations(deps.store, "alice", report)

    later = "run_later"
    assert deps.doc_index.search("alice", later, "Orrin Bio revenue") == []
    assert deps.doc_index.search("bob", first.id, "Orrin Bio revenue") == []
    assert deps.doc_index.search("alice", first.id, "Orrin Bio revenue")  # kept, not deleted
    assert report_citations(deps.store, "alice", report) == before


def test_two_owners_uploading_the_same_file_stay_apart(doc_deps):
    deps = doc_deps()
    run_a = run_with_document(deps, owner="alice")
    deps.planner_model, deps.researcher_model = planner(), researchers()
    run_b = run_with_document(deps, owner="bob")
    assert run_a.state == run_b.state == RunState.COMPLETE
    # Each owner's hits resolve only through that owner's own stores.
    for owner, run in (("alice", run_a), ("bob", run_b)):
        hits = deps.doc_index.search(owner, run.id, "revenue")
        assert hits and all(deps.store.get_passages(owner, h.passage.observation_id,
                                                    [h.passage.index]) for h in hits)
    assert deps.doc_index.search("bob", run_a.id, "revenue") == []
    assert deps.doc_index.search("alice", run_b.id, "revenue") == []


# --- failure paths ------------------------------------------------------------------------

def test_an_unreadable_document_is_logged_and_the_run_continues_on_web(doc_deps):
    deps = doc_deps(planner_model=fakes.planner(), researcher_model=fakes.researcher())
    broken = pdf("broken.pdf")
    run = run_with_document(deps, uploads=[broken])
    assert run.state == RunState.COMPLETE
    (bad,) = [e.payload for e in events(deps, run, "document_unreadable")]
    assert bad["filename"] == "broken.pdf"
    assert [e.payload for e in events(deps, run, "documents_unavailable")]


def test_a_documents_only_run_whose_documents_are_unreadable_fails(doc_deps):
    deps = doc_deps()
    run = run_with_document(deps, uploads=[pdf("broken.pdf")], web=False)
    assert run.state == RunState.FAILED
    (failed,) = [e.payload for e in events(deps, run, "run_failed")]
    assert "no uploaded document could be read" in failed["reason"]


@pytest.mark.parametrize("uploads, match", [
    ([pdf(f"r{n}.pdf") for n in range(11)], "at most 10"),
    ([Upload(filename="big.pdf", data=b"%PDF-" + b"x" * (20 * 1024 * 1024))], "larger than 20 MB"),
    ([Upload(filename="notes.txt", data=b"hello")], "not a PDF"),
])
def test_uploads_over_the_limits_are_rejected_before_a_run_exists(doc_deps, uploads, match):
    deps = doc_deps()
    with pytest.raises(SubmissionRejected, match=match):
        run_brief(deps, "alice", brief_with(uploads), uploads=uploads)
    assert deps.repos.runs.list_for_owner("alice") == []
    assert deps.chunker.calls == []


def test_documents_without_the_document_infrastructure_cannot_run(make_deps):
    deps = make_deps()  # no chunker, no doc_chunks index
    with pytest.raises(SubmissionRejected, match="no runnable source type"):
        run_brief(deps, "alice", brief_with([pdf()], web=False), uploads=[pdf()])


# --- review regressions -------------------------------------------------------------------

def test_a_run_reads_its_own_observation_when_another_run_uploaded_the_same_file_later(doc_deps):
    """The same file in two Runs gives one Source with two Observations. A Run must index, read
    and cite its own Observation, even when another Run's is the Source's latest."""
    from datetime import UTC, datetime

    later = doc_deps(now=lambda: datetime(2026, 10, 2, tzinfo=UTC))
    run_b = run_with_document(later)
    earlier = doc_deps(repos=later.repos, store=later.store, doc_index=later.doc_index,
                       now=lambda: datetime(2026, 10, 1, tzinfo=UTC))
    run_a = run_with_document(earlier)
    assert run_a.state == run_b.state == RunState.COMPLETE
    (obs_a,) = [e.payload["observation_id"] for e in events(earlier, run_a, "document_ingested")]
    (obs_b,) = [e.payload["observation_id"] for e in events(later, run_b, "document_ingested")]
    assert obs_a != obs_b
    assert {h.passage.observation_id for h in earlier.doc_index.search("alice", run_a.id, "revenue")} == {obs_a}
    report = earlier.repos.reports.get_for_run("alice", run_a.id)
    cited = {i["primary"]["observation_id"] for s in report.body["evidence"]["sections"]
             for i in s["items"]} - {o.id for s in earlier.repos.sources.list_for_run("alice", run_a.id)
                                     if s.kind == "web"
                                     for o in earlier.repos.observations.list_for_source("alice", s.id)}
    assert cited == {obs_a}


def test_an_unavailable_vector_store_drops_documents_and_the_run_continues_on_web(doc_deps):
    class Down(DocChunkIndex):
        def index(self, *args, **kwargs):
            raise ConnectionError("qdrant is not running")

    deps = doc_deps(planner_model=fakes.planner(), researcher_model=fakes.researcher(),
                    doc_index=Down(QdrantClient(":memory:"), HashingEmbedder(), "test-hash", 256))
    run = run_with_document(deps)
    assert run.state == RunState.COMPLETE
    (bad,) = [e.payload for e in events(deps, run, "document_unindexed")]
    assert "qdrant is not running" in bad["reason"]
    assert events(deps, run, "documents_unavailable")


def test_search_hits_show_the_name_this_run_uploaded_the_file_under(doc_deps):
    deps = doc_deps()
    first = run_with_document(deps, uploads=[pdf("q3.pdf")])
    deps.planner_model, deps.researcher_model = planner(), researchers()
    renamed = Upload(filename="renamed.pdf", data=pdf("q3.pdf").data)
    second = run_with_document(deps, uploads=[renamed])
    assert {h.source_filename for h in deps.doc_index.search("alice", second.id, "revenue")} == {"renamed.pdf"}
    assert {h.source_filename for h in deps.doc_index.search("alice", first.id, "revenue")} == {"q3.pdf"}


def test_a_resumed_run_without_its_uploads_fails_instead_of_dropping_documents(doc_deps):
    import uuid

    from insightforge_agent.domain.models import Run
    from insightforge_agent.pipeline.errors import RunFailed
    from insightforge_agent.pipeline.graph import build_graph, outer_config

    deps = doc_deps()
    run = Run(id="run_r", owner_id="alice", brief=BRIEF, state=RunState.INGESTING,
              created_at=deps.now())
    deps.repos.runs.add(run)
    thread = f"thread-{uuid.uuid4()}"
    state = {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": BRIEF,
             "allowed_types": ["web", "documents"], "has_uploads": True}
    with pytest.raises(RunFailed, match="upload them again"):
        build_graph(deps).invoke(state, outer_config(thread))  # no uploads, as after a restart


# --- review round 2: Sub-task chunk authorisation -----------------------------------------

def overreaching_reader(owner_of: dict) -> ScriptedChat:
    """The Doc Reader for s1 names every chunk of the document, including chunks only s2's
    search returned; s2's reader names just its own hits. Web Sub-tasks behave normally."""
    def reply(messages) -> AIMessage:
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        last = messages[-1]
        task = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        if "Document Reader" in system and isinstance(last, ToolMessage):
            hits = json.loads(str(last.content))
            ids = [h["chunk_id"] for h in hits]
            if task.startswith("SUBTASK_ID=s1 "):
                obs = hits[0]["chunk_id"].split(":")[0]
                ids = [f"{obs}:{i}" for i in range(len(REPORT))]
                owner_of["s1_named"] = ids
            return AIMessage(content=f"RESULT: chunks | chunks: {', '.join(ids)}")
        return researchers().reply(messages)
    return ScriptedChat(reply=reply)


def test_a_subtask_may_return_only_chunks_its_own_searches_returned(doc_deps):
    named: dict = {}
    deps = doc_deps(researcher_model=overreaching_reader(named))
    snapshots = []
    uploads = [pdf()]
    run = run_brief(deps, "alice", brief_with(uploads), uploads=uploads, on_state=snapshots.append)
    assert run.state == RunState.COMPLETE

    searched: dict[str, set[str]] = {}
    for e in events(deps, run, "doc_search"):
        searched.setdefault(e.payload["subtask_id"], set()).update(e.payload["chunk_ids"])
    assert set(searched) == {"s1", "s2"}  # every search names the Sub-task that made it
    only_s2 = searched["s2"] - searched["s1"]
    assert only_s2 and only_s2 <= set(named["s1_named"])  # s1 named chunks it was never given

    research = next(s["research"] for s in reversed(snapshots) if "research" in s)
    accepted = {r["subtask_id"]: {f"{p['observation_id']}:{p['index']}"
                                  for ref in r["source_refs"] for p in ref["passages"]}
                for r in research["results"]}
    assert accepted["s1"] == searched["s1"]  # only s1's own hits survive
    assert accepted["s1"].isdisjoint(only_s2)
    assert accepted["s2"] <= searched["s2"]


def test_a_search_without_a_subtask_authorises_nothing(doc_deps):
    from insightforge_agent.pipeline.graph import returned_chunks

    deps = doc_deps()
    deps.log("alice", "run_x", "doc_search", subtask_id=None, query="q", chunk_ids=["obs_x:1"])
    deps.log("alice", "run_x", "doc_search", subtask_id="s2", query="q", chunk_ids=["obs_x:2"])
    assert returned_chunks(deps, "alice", "run_x") == {"s2": {"obs_x:2"}}


# --- review round 2: chunk extraction failures are ChunkingError ---------------------------

class _Converted:
    status = None
    document = object()


def docling_with(monkeypatch, chunker_cls):
    """A DoclingChunker whose conversion succeeds and whose chunking is `chunker_cls`."""
    from docling.datamodel.base_models import ConversionStatus
    from docling_core.transforms import chunker as docling_chunker

    from insightforge_agent.agents.doc_reader import DoclingChunker

    class Converter:
        def convert(self, *args, **kwargs):
            result = _Converted()
            result.status = ConversionStatus.SUCCESS
            return result

    monkeypatch.setattr(docling_chunker, "HierarchicalChunker", chunker_cls)
    reader = DoclingChunker()
    monkeypatch.setattr(reader, "_converter_once", lambda: Converter())
    return reader


class _FailingChunker:
    def chunk(self, document):
        raise ValueError("malformed layout tree")


class _BadMetadataChunker:
    def chunk(self, document):
        class Chunk:
            text = "Revenue rose."
            meta = None  # provenance cannot be read
        return [Chunk()]


@pytest.mark.parametrize("chunker_cls, detail", [
    (_FailingChunker, "ValueError: malformed layout tree"),
    (_BadMetadataChunker, "AttributeError"),
])
def test_chunking_and_metadata_failures_become_chunking_errors(monkeypatch, chunker_cls, detail):
    reader = docling_with(monkeypatch, chunker_cls)
    with pytest.raises(ChunkingError, match=detail):
        reader.chunk("report.pdf", b"%PDF-1.7")


def test_an_interrupt_while_chunking_is_not_swallowed(monkeypatch):
    class Interrupted:
        def chunk(self, document):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        docling_with(monkeypatch, Interrupted).chunk("report.pdf", b"%PDF-1.7")


def test_a_run_continues_on_web_when_chunk_extraction_fails(doc_deps, monkeypatch):
    deps = doc_deps(planner_model=fakes.planner(), researcher_model=fakes.researcher(),
                    chunker=docling_with(monkeypatch, _FailingChunker))
    run = run_with_document(deps)
    assert run.state == RunState.COMPLETE
    (bad,) = [e.payload for e in events(deps, run, "document_unreadable")]
    assert "malformed layout tree" in bad["reason"]
    assert events(deps, run, "documents_unavailable")
    report = deps.repos.reports.get_for_run("alice", run.id)
    assert [c for s in report.body["draft"]["sections"] for c in s["claims"]]  # web Evidence
