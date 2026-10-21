"""T7 review: parallel Web and Document Researchers share one SQLite connection safely.

The production persistence path (`SqliteRepos`, one connection with check_same_thread=False) is
used with two web and two document Sub-tasks dispatched in one turn. A probe wraps the
repository calls the Researcher tools make and records any two that overlap in time; a short
pause inside each call widens the window so an unserialised pair would be caught."""

import threading
import time

from qdrant_client import QdrantClient

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.pipeline import fakes
from tests.pipeline.test_t7_documents import BRIEF, Chunker, events, pdf, researchers
from tests.scripted import ScriptedChat, ai, call

QUERY = {"s1": fakes.QUERIES["s1"], "s2": fakes.QUERIES["s2"],
         "s3": "Orrin Bio revenue fiscal 2025 million dollars",
         "s4": "research pipeline clinical programmes phase 3 trials 2025"}
TYPE = {"s1": "web", "s2": "web", "s3": "documents", "s4": "documents"}
AGENT = {"web": "researcher", "documents": "doc_reader"}


def planner() -> ScriptedChat:
    plan = {"plan": {
        "aspects": [{"id": f"a{n}", "name": f"Aspect {n}"} for n in range(1, 5)],
        "sub_tasks": [{"id": s, "aspect_id": f"a{n}", "query": QUERY[s], "source_type": TYPE[s],
                       "time_horizon_months": 12, "priority": n}
                      for n, s in enumerate(QUERY, start=1)]}}
    todos = call("write_todos", {"todos": [{"content": q, "status": "pending"}
                                           for q in QUERY.values()]}, "todo-1")
    tasks = [call("task", {"description": f"SUBTASK_ID={s} {q}",
                           "subagent_type": AGENT[TYPE[s]]}, f"research-{s}")
             for s, q in QUERY.items()]  # all four in one turn: they run in parallel
    return ScriptedChat(script=[ai(call("submit_plan", plan, "plan-1")), ai(text="ok"),
                                ai(todos, *tasks), ai(text="Dispatched.")])


class OverlapProbe:
    """Records the largest number of distinct threads inside wrapped repository calls at once.
    A call nested in another on the same thread (observations.add reads back with get) counts
    once."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._depth: dict[int, int] = {}
        self.max_active = 0
        self.threads: set[int] = set()

    def wrap(self, obj, name: str) -> None:
        real = getattr(obj, name)

        def probed(*args, **kwargs):
            me = threading.get_ident()
            with self._guard:
                self._depth[me] = self._depth.get(me, 0) + 1
                self.max_active = max(self.max_active, len(self._depth))
                self.threads.add(me)
            try:
                time.sleep(0.01)
                return real(*args, **kwargs)
            finally:
                with self._guard:
                    self._depth[me] -= 1
                    if not self._depth[me]:
                        del self._depth[me]

        setattr(obj, name, probed)


def test_parallel_web_and_document_researchers_never_overlap_on_sqlite(make_deps, tmp_path):
    repos = SqliteRepos(str(tmp_path / "insightforge.db"))
    probe = OverlapProbe()
    for repo, name in [(repos.events, "append"), (repos.sources, "add"),
                       (repos.observations, "add"), (repos.observations, "get"),
                       (repos.passages, "add_all")]:
        probe.wrap(repo, name)
    deps = make_deps(
        repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
        planner_model=planner(), researcher_model=researchers(), chunker=Chunker(),
        doc_index=DocChunkIndex(QdrantClient(":memory:"), HashingEmbedder(), "test-hash", 256))
    upload = pdf()
    brief = Brief(text=BRIEF, source_toggles={"web": True, "documents": True},
                  document_ids=[upload.document_id])
    try:
        run = run_brief(deps, "alice", brief, uploads=[upload])
        assert run.state == RunState.COMPLETE, [e.payload for e in events(deps, run, "run_failed")]
        assert len(probe.threads) > 1, "the Researchers did not run in parallel"
        assert probe.max_active == 1, "two repository calls ran at once on one connection"
        # Everything the parallel tools wrote is in SQLite and readable.
        searched = {e.payload["subtask_id"] for e in events(deps, run, "doc_search")}
        assert searched == {"s3", "s4"}
        assert len(events(deps, run, "source_summarized")) >= 2
        kinds = {s.kind for s in repos.sources.list_for_run("alice", run.id)}
        assert kinds == {"web", "pdf"}
        assert {r.status.value for r in repos.subtasks.list("alice", run.id)} == {"SUCCEEDED"}
    finally:
        repos.close()
