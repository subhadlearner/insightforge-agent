"""Real uploaded-document research for the web E2E backend (T20 B6). Not production code.

The production document stack, offline: Docling converts and chunks the uploaded PDF, the
local fastembed model (`BAAI/bge-small-en-v1.5`) embeds the chunks into an in-process Qdrant,
and the Doc Reader's search returns them. Only the chat models are scripted: the Planner plans
document Sub-tasks for the PDF Brief (`PDF_BRIEF_MARKER` in the Brief itself, never in earlier
Run summaries), and the Doc Reader names the top chunks its own search returned. Extraction,
Synthesis, the Writer and the Fact-Checker are the same scripted models every other E2E Run
uses, over the real retrieved chunks.

The model files come from the local cache only (`HF_HUB_OFFLINE=1`; fetch them once with
`uv run insightforge-agent models download`). Without them the stack is not wired and
`/__test__/capabilities` says why, so the browser test skips with that reason instead of
pretending. `E2E_REAL_DOCUMENTS=1` makes a missing cache a startup error instead; `0` never
loads the models.

Every outbound (non-loopback) connection and DNS lookup the backend process attempts is
blocked and recorded (`tests/network_guard.py`); the browser test asserts there were none."""

import json
import os
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests import network_guard
from tests.api.support import planner_reply
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, ai, call

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
PDF_BRIEF_MARKER = "Northwind Health"
DOC_QUERIES = {
    "s1": "How much revenue did the company report for fiscal 2025?",
    "s2": "How much was spent on research and development?",
    "s3": "Which supplier dependency is a principal risk?",
}
DOC_PLAN = {"plan": {
    "aspects": [{"id": "a1", "name": "Revenue"}, {"id": "a2", "name": "Research and development"},
                {"id": "a3", "name": "Risks"}],
    "sub_tasks": [{"id": sid, "aspect_id": f"a{n}", "query": q, "source_type": "documents",
                   "time_horizon_months": 12, "priority": n}
                  for n, (sid, q) in enumerate(DOC_QUERIES.items(), start=1)]}}
CHUNKS_NAMED = 2  # the Doc Reader names its search's top chunks, best first
_LISTED = re.compile(r"^- ([A-Za-z0-9_]+): (.*) \[subagent_type ([a-z_]+)\]$", re.MULTILINE)
_blocked: list[str] = []


class _Patch:
    """`network_guard.install` patches through a pytest monkeypatch; this process has none."""

    @staticmethod
    def setattr(target, name, value) -> None:
        setattr(target, name, value)


def guard_network() -> list[str]:
    """Block and record every outbound connection of this process; returns the record."""
    network_guard.install(_Patch(), _blocked)
    return _blocked


def _brief_of(plan_message: str) -> str:
    """The Brief of a PLAN message, without the earlier Run summaries appended to it."""
    return plan_message.split("Brief: ", 1)[-1].split("\nEarlier Runs", 1)[0]


def planner(messages) -> AIMessage:
    """The E2E Planner, plus document plans for the PDF Brief. RESEARCH and REPAIR dispatch
    exactly the Sub-tasks the message lists, with the subagent it names."""
    last = messages[-1]
    if isinstance(last, HumanMessage):
        text = str(last.content)
        if text.startswith("PLAN") and PDF_BRIEF_MARKER in _brief_of(text):
            return ai(call("submit_plan", DOC_PLAN, f"plan-doc-{len(messages)}"))
        listed = _LISTED.findall(text)
        if text.startswith(("RESEARCH", "REPAIR")) and any(a == "doc_reader" for *_, a in listed):
            return ai(*[call("task", {"description": f"SUBTASK_ID={sid} {query}",
                                      "subagent_type": agent}, f"doc-{sid}-{len(messages)}")
                        for sid, query, agent in listed])
    return planner_reply(messages)


def researcher(messages) -> AIMessage:
    """The E2E web Researcher, plus the Doc Reader: one search with its Sub-task's query, then a
    RESULT naming the top chunks that search returned."""
    system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
    if "Document Reader" not in system:
        return fakes.researcher().reply(messages)
    last = messages[-1]
    if isinstance(last, ToolMessage):
        hits = json.loads(str(last.content))[:CHUNKS_NAMED]
        return AIMessage(content="RESULT: relevant chunks | chunks: "
                         + ", ".join(h["chunk_id"] for h in hits))
    text = "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
    query = text.split("SUBTASK_ID=", 1)[1].split(" ", 1)[1].strip()
    return ai(call("doc_search", {"query": query}, f"doc_search-{abs(hash(text))}"))


def load(mode: str) -> tuple[dict, dict]:
    """(Deps overrides, capabilities). Mode `auto` wires the real stack when its model files
    are cached, `1` requires it, `0` skips it."""
    if mode == "0":
        return {}, {"real_documents": False, "reason": "E2E_REAL_DOCUMENTS=0"}
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        import huggingface_hub.constants
        from qdrant_client import QdrantClient

        from insightforge_agent.agents.doc_reader import DoclingChunker
        from insightforge_agent.embeddings import FastEmbedEmbedder
        from insightforge_agent.pdf_writer import make_pdf
        from insightforge_agent.stores.doc_chunks import DocChunkIndex

        huggingface_hub.constants.HF_HUB_OFFLINE = True
        embedder = FastEmbedEmbedder(EMBEDDING_MODEL)
        embedder.embed(["load"])
        chunker = DoclingChunker()
        chunker.chunk("load.pdf", make_pdf([[(16, "Load"), (10, "Loads the layout model.")]]))
    except Exception as e:  # noqa: BLE001 - a missing model file has no single error type
        if mode == "1":
            raise
        return {}, {"real_documents": False,
                    "reason": f"the Docling and fastembed model files are not in the local cache "
                              f"(run `uv run insightforge-agent models download`): "
                              f"{type(e).__name__}: {e}"[:600]}
    index = DocChunkIndex(QdrantClient(":memory:"), embedder, EMBEDDING_MODEL,
                          embedder.dimensions)
    overrides = {"chunker": chunker, "doc_index": index, "embedder": embedder,
                 "planner_model": ScriptedChat(reply=planner),
                 "researcher_model": ScriptedChat(reply=researcher)}
    return overrides, {"real_documents": True, "reason": None,
                       "chunker": type(chunker).__name__, "embedder": EMBEDDING_MODEL}
