"""The Document Reader: PDF chunking (Docling), its search tool, prompt and subagent definition.

The tool searches the Run's own uploads in `doc_chunks` (k = 8) and hands the Doc Reader only
chunk IDs with their Source, file name, page and relevance score, never chunk text. Each search
is recorded as a durable `doc_search` event so the graph accepts only chunk IDs the tool really
returned."""

import io
import json
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from insightforge_agent.domain.documents import chunk_id
from insightforge_agent.domain.models import RunEvent
from insightforge_agent.domain.plan import DOC_READER
from insightforge_agent.domain.repositories import RunEventRepository
from insightforge_agent.stores.doc_chunks import DEFAULT_K, DocChunkIndex
from insightforge_agent.stores.source_store import PassageDraft

DOC_SEARCH_EVENT = "doc_search"  # durable record of {query, chunk_ids} a search returned
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
            result = self._converter_once().convert(
                DocumentStream(name=filename, stream=io.BytesIO(data)), raises_on_error=False)
        except Exception as e:  # noqa: BLE001 - any parser failure makes the document unreadable
            raise ChunkingError(f"{filename}: {type(e).__name__}: {e}") from e
        if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
            raise ChunkingError(f"{filename}: conversion {result.status.value}")
        drafts = []
        for c in HierarchicalChunker().chunk(result.document):
            text = " ".join(c.text.split())
            if not text:
                continue
            pages = sorted({p.page_no for item in c.meta.doc_items for p in item.prov})
            headings = c.meta.headings or []
            drafts.append(PassageDraft(text, page=pages[0] if pages else None,
                                       section_heading=headings[-1] if headings else None))
        if not drafts:
            raise ChunkingError(f"{filename}: no text found (scanned PDFs are not read)")
        return drafts


def make_doc_search_tool(
    *,
    owner_id: str,
    run_id: str,
    index: DocChunkIndex,
    events: RunEventRepository,
    now: Callable[[], datetime],
    k: int = DEFAULT_K,
) -> BaseTool:
    lock = threading.Lock()  # one SQLite connection is shared by parallel Researchers

    @tool
    def doc_search(query: str) -> str:
        """Search the user's uploaded documents for the query and return the most relevant
        chunks as JSON {chunk_id, source_id, source_filename, page, relevance_score}."""
        hits = index.search(owner_id, run_id, query, k)
        with lock:
            events.append(RunEvent(owner_id=owner_id, run_id=run_id, type=DOC_SEARCH_EVENT,
                                   payload={"query": query[:500],
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
