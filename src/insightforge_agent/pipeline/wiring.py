"""Builds a Run's dependencies from Settings: real models, search, fetching and documents."""

from datetime import timedelta
from typing import Any

from qdrant_client import QdrantClient

from insightforge_agent.agents.doc_reader import DoclingChunker
from insightforge_agent.agents.web import HttpFetcher, build_search
from insightforge_agent.config import Settings
from insightforge_agent.domain.evidence import Thresholds
from insightforge_agent.embeddings import FastEmbedEmbedder
from insightforge_agent.llm import build_chat_model, native_json
from insightforge_agent.pipeline.deps import Deps, TimeoutScheduler
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.report_sections import ReportSectionIndex
from insightforge_agent.stores.source_store import SourceStore


def build_qdrant(settings: Settings) -> QdrantClient:
    """The Qdrant server at QDRANT_URL (docker compose), or in-process with ":memory:".
    Constructing the client opens no connection."""
    if settings.qdrant_url == ":memory:":
        return QdrantClient(":memory:")
    return QdrantClient(url=settings.qdrant_url)


def build_deps(settings: Settings, repos: Any, checkpointer: Any,
               timeouts: TimeoutScheduler | None = None,
               qdrant: QdrantClient | None = None) -> Deps:
    """`qdrant`: a client the caller owns (and closes); by default one from QDRANT_URL."""
    # One local embedding model for everything that embeds (design.md section 10): Doc Reader
    # search, Memory researcher search, Evidence relevance and Fact-Checker similarity. It
    # loads on first use.
    embedder = FastEmbedEmbedder(settings.embedding_model)
    qdrant = qdrant if qdrant is not None else build_qdrant(settings)
    return Deps(
        repos=repos,
        store=SourceStore(repos.sources, repos.observations, repos.passages),
        planner_model=build_chat_model("planner", settings),
        writer_model=build_chat_model("writer", settings),
        light_model=build_chat_model("light", settings),
        researcher_model=build_chat_model("light", settings),
        search=build_search(settings),
        fetcher=HttpFetcher(),
        checkpointer=checkpointer,
        embedder=embedder,
        doc_index=DocChunkIndex(qdrant, embedder, settings.embedding_model, embedder.dimensions),
        report_index=ReportSectionIndex(qdrant, embedder, settings.embedding_model,
                                        embedder.dimensions),
        chunker=DoclingChunker(),
        evidence_budget_tokens=settings.evidence_budget_tokens,
        passage_token_cap=settings.passage_token_cap,
        run_token_cap=settings.run_token_cap,
        fact_check_rate=settings.fact_check_rate,
        fact_check_minimum=settings.fact_check_minimum,
        similarity_threshold=settings.similarity_threshold,
        native_json=native_json if settings.draft_structured_output else None,
        draft_passage_cap=settings.draft_passage_cap,
        timeouts=timeouts,
        clarification_timeout=timedelta(minutes=settings.clarification_timeout_minutes),
        thresholds=Thresholds(
            credibility_high=settings.credibility_high,
            credibility_medium=settings.credibility_medium,
            recency_fresh_days=settings.recency_fresh_days,
            recency_recent_days=settings.recency_recent_days,
        ),
    )
