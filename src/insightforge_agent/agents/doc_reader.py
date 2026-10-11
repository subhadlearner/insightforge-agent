"""The Document Reader: PDF chunking (Docling), its search tool, prompt and subagent definition.

The tool searches the Run's own uploads in `doc_chunks` (k = 8) and hands the Doc Reader only
chunk IDs with their Source, file name, page and relevance score, never chunk text. Each search
is recorded as a durable `doc_search` event naming the Sub-task that searched, so the graph
accepts for a Sub-task only chunk IDs that Sub-task's own searches returned."""

import io
from contextlib import AbstractContextManager
import json
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool

from insightforge_agent.domain.documents import chunk_id
from insightforge_agent.domain.models import RunEvent
from insightforge_agent.domain.plan import DOC_READER, dispatched_id
from insightforge_agent.domain.repositories import RunEventRepository
from insightforge_agent.stores.doc_chunks import DEFAULT_K, DocChunkIndex
from insightforge_agent.stores.source_store import PassageDraft

DOC_SEARCH_EVENT = "doc_search"  # durable record of {subtask_id, query, chunk_ids} a search returned
UPLOADED_DOCUMENT_CREDIBILITY = 0.7

DOC_READER_PROMPT = """You are a Document Reader. Your task description starts with
"SUBTASK_ID=<id>" followed by your research query. Call `doc_search` with that query (once
more with a refined query only if the first returns nothing useful). It returns the most
relevant chunks of the user's uploaded documents as {chunk_id, source_id, source_filename,
page, relevance_score}. Then reply with exactly one line:
"RESULT: <one sentence on what you found> | chunks: <chunk_id>, <chunk_id>, ..."
listing only chunk_ids that `doc_search` returned and that bear on the query. If nothing
useful was found, reply "FAILED: <reason>"."""


class ChunkingError(Exception):
    """A document could not be read or produced no text."""


class DocumentChunker(Protocol):
    def chunk(self, filename: str, data: bytes) -> list[PassageDraft]:
        """The document's chunks in reading order. Raises ChunkingError."""
        ...


class DoclingChunker:
    """Docling extracts the PDF and chunks it by paragraph where the structure allows,
    keeping the page and the nearest section heading (design.md section 4). OCR is off:
    scanned PDFs without a text layer produce no chunks and are reported as unreadable."""

    def __init__(self) -> None:
        self._converter = None
        self._lock = threading.Lock()

    def _converter_once(self):
        with self._lock:
            if self._converter is None:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
                self._converter = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
            return self._converter

    def chunk(self, filename: str, data: bytes) -> list[PassageDraft]:
        from docling.datamodel.base_models import ConversionStatus, DocumentStream
        from docling_core.transforms.chunker import HierarchicalChunker

        try:
            drafts = self._read(filename, data, DocumentStream, ConversionStatus, HierarchicalChunker)
        except ChunkingError:
            raise
        except Exception as e:  # noqa: BLE001 - conversion, chunking or metadata: unreadable PDF
            # KeyboardInterrupt and SystemExit are BaseException, not Exception: they still stop
            # the process.
            raise ChunkingError(f"{filename}: {type(e).__name__}: {e}") from e
        if not drafts:
            raise ChunkingError(f"{filename}: no text found (scanned PDFs are not read)")
        return drafts

    def _read(self, filename, data, document_stream, status, chunker) -> list[PassageDraft]:
        result = self._converter_once().convert(
            document_stream(name=filename, stream=io.BytesIO(data)), raises_on_error=False)
        if result.status not in (status.SUCCESS, status.PARTIAL_SUCCESS):
            raise ChunkingError(f"{filename}: conversion {result.status.value}")
        drafts = []
        for c in chunker().chunk(result.document):
            text = " ".join(c.text.split())
            if not text:
                continue
            pages = sorted({p.page_no for item in c.meta.doc_items for p in item.prov})
            headings = c.meta.headings or []
            drafts.append(PassageDraft(text, page=pages[0] if pages else None,
                                       section_heading=headings[-1] if headings else None))
        return drafts


def searching_subtask(messages) -> str | None:
    """The Sub-task a Doc Reader works on: the SUBTASK_ID of the task description it was given
    (its first human message), which the dispatch guard has checked is canonical. Never taken
    from a tool argument the model chose."""
    first = next((m for m in messages if isinstance(m, HumanMessage)), None)
    return dispatched_id(str(first.content)) if first is not None else None


def make_doc_search_tool(
    *,
    owner_id: str,
    run_id: str,
    index: DocChunkIndex,
    events: RunEventRepository,
    now: Callable[[], datetime],
    lock: AbstractContextManager,
    k: int = DEFAULT_K,
) -> BaseTool:
    """`lock` serialises repository writes with every other Researcher tool of the Run (the
    repositories' shared lock); the vector search itself runs outside it."""

    @tool
    def doc_search(query: str, runtime: ToolRuntime) -> str:
        """Search the user's uploaded documents for the query and return the most relevant
        chunks as JSON {chunk_id, source_id, source_filename, page, relevance_score}."""
        hits = index.search(owner_id, run_id, query, k)
        subtask_id = searching_subtask(runtime.state.get("messages", []))
        with lock:
            events.append(RunEvent(owner_id=owner_id, run_id=run_id, type=DOC_SEARCH_EVENT,
                                   payload={"subtask_id": subtask_id, "query": query[:500],
                                            "chunk_ids": [chunk_id(h.passage) for h in hits]},
                                   created_at=now()))
        return json.dumps([{"chunk_id": chunk_id(h.passage), "source_id": h.source_id,
                            "source_filename": h.source_filename, "page": h.page,
                            "relevance_score": h.relevance_score} for h in hits])

    return doc_search


def doc_reader_subagent(tools: list[BaseTool], model: BaseChatModel) -> dict:
    return {
        "name": DOC_READER,
        "description": "Searches the user's uploaded documents for one Sub-task and replies "
                       "RESULT: or FAILED:.",
        "system_prompt": DOC_READER_PROMPT,
        "tools": tools,
        "model": model,
    }
