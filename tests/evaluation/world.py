"""A scripted world for `insightforge-agent eval`: the production pipeline, repositories and
stores, with scripted models, recorded search pages and a scripted chunker in place of the
providers, Tavily and Docling. Nothing here bypasses `run_brief`; only the models are fakes.

The Planner reads its PLAN message the way a real one would: the allowed source types and the
Brief's own subject decide the plan, so one Deps serves every sample brief. Each brief
researches its own recorded material: the cloud, semiconductor, solar and medical-devices
pages in `tests/fixtures/search/eval_domains.json` (fictional test data, marked as such), the
EV pages of the T3 fixture for the EV briefs, and the Northwind Health sample PDF's own text
for the document brief. Scripted models copy Passage sentences into Evidence and Claims, so
this world shows that each brief's research path and provenance stay on its subject; it cannot
show that a model understands that subject (T18 runs real models).

Per provider, a brief whose text holds a `fail_all` keyword gets only failing Sub-tasks (a
Failed Run), one with a `fail_one` keyword loses its last Sub-task (a Run that completes with
a gap), and `drift` makes every re-fetch of a page return different text (the Fact-Checker
finds no match, so its Claims are unverified). `overrides` sets Deps fields (pipeline
settings) per provider; `substitute` answers a search query with another query's results."""

import importlib.util
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from qdrant_client import QdrantClient

from insightforge_agent.agents.doc_reader import ChunkingError
from insightforge_agent.agents.web import FetchError, Page, PaywalledError, SearchHit
from insightforge_agent.config import Settings
from insightforge_agent.domain.plan import dispatched_id
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.report_sections import ReportSectionIndex
from insightforge_agent.stores.source_store import PassageDraft, SourceStore
from tests.pipeline import fakes
from tests.pipeline.test_t8_memory import MEMORY_QUERIES
from tests.pipeline.test_t8_memory import writer as historical_writer
from tests.scripted import ScriptedChat, ai, call

ROOT = Path(__file__).resolve().parents[2]
FAIL = "FAIL-ME"  # a Researcher given this in its task reports failure (tests.pipeline.fakes)
_LISTING = re.compile(r"^- (\w+): (.+) \[subagent_type (\w+)\]$", re.MULTILINE)

# Web briefs, by a phrase of their own Brief: one Sub-task per main Aspect it asks about, each
# named as the brief's expected section. Anything else researches the EV pages.
WEB_PLANS = [
    ("Amazon Web Services", [
        ("Revenue growth", "AWS Azure Google Cloud revenue growth 2025"),
        ("Market share", "cloud infrastructure market share 2025 AWS Azure Google Cloud"),
        ("AI infrastructure investment",
         "AI infrastructure capital expenditure 2025 Amazon Microsoft Alphabet")]),
    ("export controls", [
        ("Export control changes", "US export controls AI chips China 2025 rule changes"),
        ("Revenue impact", "Nvidia AMD China revenue impact export controls 2025"),
        ("Product and strategy responses", "Nvidia AMD China specific AI chip products 2025")]),
    ("solar photovoltaic", [
        ("Installed capacity in 2025", "global solar PV installed capacity 2025 gigawatts"),
        ("Leading markets", "leading solar markets 2025 China United States India installations"),
        ("2026 outlook", "solar PV installation forecast 2026 outlook")]),
]
EV_PLAN = [("Sales", fakes.QUERIES["s1"]), ("Pricing", fakes.QUERIES["s2"]),
           ("Batteries", fakes.QUERIES["s3"])]
DOCUMENT_PLAN = [  # (Aspect, query, source type) for the annual-report brief
    ("Revenue and segment performance",
     "Northwind Health revenue and segment results fiscal 2025", "documents"),
    ("Research and development pipeline",
     "Northwind research and development spending and the NW-200 trial", "documents"),
    ("Principal risks",
     "Northwind principal risks titanium supplier pricing NW-200 approval", "documents"),
    ("Market comparison", "medical devices market growth 2025", "web"),
]


def _plan_for(allowed: list[str], brief: str, fail_all, fail_one) -> list[tuple]:
    if "documents" in allowed:
        tasks = [(f"s{n}", f"a{n}", name, q, t)
                 for n, (name, q, t) in enumerate(DOCUMENT_PLAN, start=1)]
    elif "memory" in allowed:
        tasks = [("s1", "a1", "Sales", fakes.QUERIES["s1"], "web"),
                 ("s2", "a2", "Pricing", fakes.QUERIES["s2"], "web"),
                 ("m1", "a3", "Earlier findings", MEMORY_QUERIES["m1"], "memory")]
    else:
        plan = next((p for phrase, p in WEB_PLANS if phrase in brief), EV_PLAN)
        tasks = [(f"s{n}", f"a{n}", name, q, "web")
                 for n, (name, q) in enumerate(plan, start=1)]
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


_NOT_NAMES = {"The", "In", "A", "An", "Its", "This", "New"}


def light() -> ScriptedChat:
    """`fakes.light`, without its EV assumptions: an extracted fact's entities are the names
    its own sentence states, and a Source summary names the page it summarises."""
    base = fakes.light()

    def reply(messages) -> AIMessage:
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        if system.startswith("You extract facts"):
            facts = [{"passage_index": int(i), "statement": text.strip(),
                      "entities": [w for w in dict.fromkeys(
                          re.findall(r"\b[A-Z][A-Za-z0-9-]+", text)) if w not in _NOT_NAMES]}
                     for i, text in re.findall(r"\[(\d+)\] (.+)", user)]
            return AIMessage(content=json.dumps({"facts": facts}))
        title = re.search(r"^Title: (.+)$", user, re.MULTILINE)
        if user.startswith("Query:") and title:
            return AIMessage(content=f"A recorded page: {title.group(1)}.")
        return base.reply(messages)
    return ScriptedChat(reply=reply)


def _sample_documents():
    spec = importlib.util.spec_from_file_location(
        "make_sample_documents", ROOT / "samples" / "documents" / "make_sample_documents.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SampleDocumentChunker:
    """Stands in for Docling on the sample PDFs: one chunk per paragraph of the generator's
    own text (`samples/documents/make_sample_documents.py`), with its page and heading."""

    def __init__(self) -> None:
        self._module = _sample_documents()

    def chunk(self, filename: str, data: bytes) -> list[PassageDraft]:
        if filename not in self._module.DOCUMENTS:
            raise ChunkingError(f"{filename}: not a sample document")
        pages, _ = self._module.DOCUMENTS[filename]
        chunks, heading = [], None
        for number, page in enumerate(pages, start=1):
            for size, text in page:
                if size == self._module.H:
                    heading = text
                elif size == self._module.P:
                    chunks.append(PassageDraft(text, page=number, section_heading=heading))
        return chunks


class RecordedPages:
    """Recorded search over the EV and the domain fixtures. With `drift`, every fetch of a
    page after its first returns other text; `substitute` maps a query to another's results."""

    def __init__(self, drift: bool = False, substitute: dict[str, str] | None = None) -> None:
        self._queries, self._pages = {}, {}
        for name in ("ev_market.json", "eval_domains.json"):
            data = json.loads((fakes.FIXTURES / name).read_text(encoding="utf-8"))
            self._queries.update(data["queries"])
            self._pages.update(data["pages"])
        self._drift, self._substitute = drift, substitute or {}
        self._fetched: set[str] = set()

    def search(self, query: str, limit: int) -> list[SearchHit]:
        hits = self._queries[self._substitute.get(query, query)]
        return [SearchHit(h["url"], h["title"]) for h in hits][:limit]

    def fetch(self, url: str) -> Page:
        page = self._pages[url]
        if page.get("paywalled"):
            raise PaywalledError(url)
        if page.get("error"):
            raise FetchError(url)
        if self._drift and url in self._fetched:
            return Page(url=url, title=page["title"],
                        body="This page now describes an unrelated gardening product.")
        self._fetched.add(url)
        return Page(url=url, title=page["title"], body=page["body"])


@dataclass
class World:
    fail_all: dict[str, tuple[str, ...]] = field(default_factory=dict)  # provider -> keywords
    fail_one: dict[str, tuple[str, ...]] = field(default_factory=dict)
    drift: frozenset[str] = frozenset()  # providers whose re-fetches drift
    overrides: dict[str, dict] = field(default_factory=dict)  # provider -> Deps fields
    substitute: dict[str, str] = field(default_factory=dict)  # query -> query whose hits to return
    built: dict[str, Deps] = field(default_factory=dict)

    def make_deps(self, settings: Settings, repos, checkpointer, qdrant: QdrantClient) -> Deps:
        provider = settings.llm_provider
        pages = RecordedPages(provider in self.drift, self.substitute)
        embedder = HashingEmbedder()
        deps = Deps(
            repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
            planner_model=planner(self.fail_all.get(provider, ()),
                                  self.fail_one.get(provider, ())),
            writer_model=historical_writer(), light_model=light(),
            researcher_model=researchers(), search=pages, fetcher=pages,
            checkpointer=checkpointer, embedder=embedder, chunker=SampleDocumentChunker(),
            doc_index=DocChunkIndex(qdrant, embedder, "test-hash", 256),
            report_index=ReportSectionIndex(qdrant, embedder, "test-hash", 256),
            **self.overrides.get(provider, {}))
        self.built[provider] = deps
        return deps
