"""A scripted world for `insightforge-agent eval`: the production pipeline, repositories and
stores, with scripted models, recorded search pages and a scripted chunker in place of the
providers, Tavily and Docling. Nothing here bypasses `run_brief`; only the models are fakes.

The Planner reads its PLAN message the way a real one would: the allowed source types decide
the plan (documents, memory or web), so one Deps serves every sample brief. Per provider, a
brief whose text holds a `fail_all` keyword gets only failing Sub-tasks (a Failed Run), one
with a `fail_one` keyword loses its last Sub-task (a Run that completes with a gap), and
`drift` makes every re-fetch of a page return different text (the Fact-Checker finds no match,
so its Claims are unverified). `overrides` sets Deps fields (pipeline settings) per provider."""

import json
import re
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from qdrant_client import QdrantClient

from insightforge_agent.agents.web import Page
from insightforge_agent.config import Settings
from insightforge_agent.domain.plan import dispatched_id
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.report_sections import ReportSectionIndex
from insightforge_agent.stores.source_store import SourceStore
from tests.pipeline import fakes
from tests.pipeline.test_t7_documents import DOC_QUERIES, Chunker
from tests.pipeline.test_t8_memory import MEMORY_QUERIES
from tests.pipeline.test_t8_memory import writer as historical_writer
from tests.scripted import ScriptedChat, ai, call

FAIL = "FAIL-ME"  # a Researcher given this in its task reports failure (tests.pipeline.fakes)
_LISTING = re.compile(r"^- (\w+): (.+) \[subagent_type (\w+)\]$", re.MULTILINE)


def _plan_for(allowed: list[str], brief: str, fail_all, fail_one) -> list[tuple]:
    if "documents" in allowed:
        tasks = [("s1", "a1", "Revenue", DOC_QUERIES["s1"], "documents"),
                 ("s2", "a2", "Pipeline", DOC_QUERIES["s2"], "documents"),
                 ("s3", "a3", "Market", fakes.QUERIES["s3"], "web")]
    elif "memory" in allowed:
        tasks = [("s1", "a1", "Sales", fakes.QUERIES["s1"], "web"),
                 ("s2", "a2", "Pricing", fakes.QUERIES["s2"], "web"),
                 ("m1", "a3", "Earlier findings", MEMORY_QUERIES["m1"], "memory")]
    else:
        tasks = [(sid, f"a{n}", name, fakes.QUERIES[sid], "web")
                 for n, (sid, name) in enumerate([("s1", "Sales"), ("s2", "Pricing"),
                                                  ("s3", "Batteries")], start=1)]
    if any(k in brief for k in fail_all):
        return [(sid, a, name, f"{FAIL} {q}", t) for sid, a, name, q, t in tasks]
    if any(k in brief for k in fail_one):
        sid, a, name, q, t = tasks[-1]
        return tasks[:-1] + [(sid, a, name, f"{FAIL} {q}", t)]
    return tasks


def planner(fail_all=(), fail_one=()) -> ScriptedChat:
    def reply(messages) -> AIMessage:
        last = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        if any(isinstance(m, AIMessage) for m in messages[last + 1:]):
            return AIMessage(content="Done.")
        text = str(messages[last].content)
        if text.startswith("PLAN"):
            allowed = re.search(r"Allowed source types: ([^.]+)\.", text).group(1).split(", ")
            brief = text.split("Brief: ", 1)[1].split("\nEarlier Runs")[0]
            tasks = _plan_for(allowed, brief, fail_all, fail_one)
            aspects = list(dict.fromkeys((a, name) for _, a, name, _, _ in tasks))
            plan = {"plan": {
                "aspects": [{"id": a, "name": name} for a, name in aspects],
                "sub_tasks": [{"id": sid, "aspect_id": a, "query": q, "source_type": t,
                               "time_horizon_months": 12, "priority": n}
                              for n, (sid, a, _, q, t) in enumerate(tasks, start=1)]}}
            return ai(call("submit_plan", plan, "plan-1"))
        listed = _LISTING.findall(text)
        todos = call("write_todos", {"todos": [{"content": q, "status": "pending"}
                                               for _, q, _ in listed]}, "todo-1")
        return ai(todos, *[call("task", {"description": f"SUBTASK_ID={sid} {q}",
                                         "subagent_type": agent}, f"research-{sid}")
                           for sid, q, agent in listed])
    return ScriptedChat(reply=reply)


def researchers() -> ScriptedChat:
    """The web Researcher of `fakes`, the Doc Reader of the T7 tests and the Memory Researcher
    of the T8 tests, told apart by their system prompts."""
    web = fakes.researcher()

    def reply(messages) -> AIMessage:
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        task = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        last = messages[-1]
        if "Document Reader" not in system and "Memory Researcher" not in system:
            return web.reply(messages)
        if FAIL in task:
            return AIMessage(content="FAILED: nothing relevant")
        query = task.split(" ", 1)[1].strip()
        if "Document Reader" in system:
            if isinstance(last, ToolMessage):
                ids = [h["chunk_id"] for h in json.loads(str(last.content))]
                return AIMessage(content=f"RESULT: relevant chunks | chunks: {', '.join(ids)}")
            return ai(call("doc_search", {"query": query}, f"doc-{dispatched_id(task)}"))
        if isinstance(last, ToolMessage):
            refs = [h["ref"] for h in json.loads(str(last.content))]
            if not refs:
                return AIMessage(content="FAILED: nothing in earlier reports")
            return AIMessage(content=f"RESULT: earlier findings | refs: {', '.join(refs)}")
        return ai(call("memory_search", {"query": query}, f"mem-{dispatched_id(task)}"))
    return ScriptedChat(reply=reply)


class DriftingPages:
    """Recorded search; with `drift`, every fetch of a page after its first returns other text."""

    def __init__(self, drift: bool) -> None:
        self._pages = fakes.FixtureSearch()
        self._drift = drift
        self._fetched: set[str] = set()

    def search(self, query, limit):
        return self._pages.search(query, limit)

    def fetch(self, url: str) -> Page:
        page = self._pages.fetch(url)
        if self._drift and url in self._fetched:
            return Page(url=url, title=page.title,
                        body="This page now describes an unrelated gardening product.")
        self._fetched.add(url)
        return page


@dataclass
class World:
    fail_all: dict[str, tuple[str, ...]] = field(default_factory=dict)  # provider -> keywords
    fail_one: dict[str, tuple[str, ...]] = field(default_factory=dict)
    drift: frozenset[str] = frozenset()  # providers whose re-fetches drift
    overrides: dict[str, dict] = field(default_factory=dict)  # provider -> Deps fields
    built: dict[str, Deps] = field(default_factory=dict)

    def make_deps(self, settings: Settings, repos, checkpointer, qdrant: QdrantClient) -> Deps:
        provider = settings.llm_provider
        pages = DriftingPages(provider in self.drift)
        embedder = HashingEmbedder()
        deps = Deps(
            repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
            planner_model=planner(self.fail_all.get(provider, ()),
                                  self.fail_one.get(provider, ())),
            writer_model=historical_writer(), light_model=fakes.light(),
            researcher_model=researchers(), search=pages, fetcher=pages,
            checkpointer=checkpointer, embedder=embedder, chunker=Chunker(),
            doc_index=DocChunkIndex(qdrant, embedder, "test-hash", 256),
            report_index=ReportSectionIndex(qdrant, embedder, "test-hash", 256),
            **self.overrides.get(provider, {}))
        self.built[provider] = deps
        return deps
