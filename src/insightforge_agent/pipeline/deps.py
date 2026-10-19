"""Everything a Run needs from outside, passed in so tests can supply scripted fakes."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel

from insightforge_agent.agents.web import PageFetcher, SearchProvider
from insightforge_agent.domain.evidence import Thresholds
from insightforge_agent.domain.models import Run, RunEvent, RunState
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.embeddings import Embedder, HashingEmbedder
from insightforge_agent.pipeline.errors import RunFailed
from insightforge_agent.stores.doc_chunks import DocChunkIndex
from insightforge_agent.stores.report_sections import ReportSectionIndex
from insightforge_agent.stores.source_store import SourceStore


class TimeoutScheduler(Protocol):
    """Durable one-shot clarification timeouts (scheduler/clarification.py in production). A
    scheduled timeout survives a restart; scheduling the same Run again replaces its job."""

    def schedule(self, owner_id: str, run_id: str, at: datetime) -> None: ...
    def cancel(self, owner_id: str, run_id: str) -> None: ...


@dataclass
class Deps:
    repos: Any  # MemoryRepos or SqliteRepos; must expose `lock`, the one lock that serialises
    # repository calls from parallel Researcher tools on a shared connection
    store: SourceStore
    planner_model: BaseChatModel
    writer_model: BaseChatModel
    light_model: BaseChatModel  # Researcher summaries and Extraction
    researcher_model: BaseChatModel
    search: SearchProvider
    fetcher: PageFetcher
    checkpointer: Any
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12]
    evidence_budget_tokens: int = 6000
    passage_token_cap: int = 3000  # per model call that reads Passage text
    thresholds: Thresholds = field(default_factory=Thresholds)
    embedder: Embedder = field(default_factory=HashingEmbedder)
    run_token_cap: int | None = None  # model tokens one Run may spend on Passage-reading calls
    writer_retries: int = 2
    fact_check_rate: float = 0.2
    fact_check_minimum: int = 5
    similarity_threshold: float = 0.85
    # T18 CS3. `native_json` (llm.native_json in production) gives drafting the provider's
    # native structured output; None keeps plain JSON. Injected so this package never imports
    # the model layer. `draft_passage_cap` bounds the Passages per drafting call; None: no bound.
    native_json: Callable[[BaseChatModel, type], Any] | None = None
    draft_passage_cap: int | None = None
    # T18 CS1A: the cited-Passage counterpart fallback of the Fact-Checker (fact_check.py).
    fact_check_counterpart: bool = True
    recursion_limit: int = 60
    # Uploaded documents (T7). Without both, the documents source type cannot be researched.
    doc_index: DocChunkIndex | None = None
    chunker: Any = None  # agents.doc_reader.DocumentChunker
    # Earlier Reports (T8). Without it the memory source type cannot be researched, and Reports
    # are finalised without being indexed, so they never become memory.
    report_index: ReportSectionIndex | None = None
    # Clarification (T9). Without a timeout scheduler a Run cannot wait for an answer, so the
    # Planner's question is refused and it must plan from the Brief as given.
    timeouts: TimeoutScheduler | None = None
    clarification_timeout: timedelta = timedelta(minutes=30)

    on_complete: Callable[[Run], object] | None = None  # scheduler post-processing, outside the graph

    _spent: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)

    def spend(self, owner_id: str, run_id: str, *texts: str) -> None:
        """Count the estimated input tokens of one model call against the Run's token cap."""
        key = (owner_id, run_id)
        self._spent[key] = self._spent.get(key, 0) + sum(estimate_tokens(t) for t in texts)
        if self.run_token_cap is not None and self._spent[key] > self.run_token_cap:
            raise RunFailed(f"the Run's token cap of {self.run_token_cap} was exceeded")

    def event(self, owner_id: str, run_id: str, type_: str, **payload) -> RunEvent:
        """A Run-log event stamped now, for a repository write that stores it with its change."""
        return RunEvent(owner_id=owner_id, run_id=run_id, type=type_, payload=payload,
                        created_at=self.now())

    def log(self, owner_id: str, run_id: str, type_: str, **payload) -> None:
        self.repos.events.append(self.event(owner_id, run_id, type_, **payload))

    def completed(self, run: Run) -> Run:
        """Post-processing may never change the research outcome; startup repairs omissions."""
        if run.state == RunState.COMPLETE and self.on_complete is not None:
            try:
                self.on_complete(run)
            except Exception:
                logging.getLogger(__name__).exception("Watchlist completion processing failed")
        return run
