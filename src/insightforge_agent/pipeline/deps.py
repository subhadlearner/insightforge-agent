"""Everything a Run needs from outside, passed in so tests can supply scripted fakes."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel

from insightforge_agent.agents.web import PageFetcher, SearchProvider
from insightforge_agent.domain.evidence import Thresholds
from insightforge_agent.domain.models import RunEvent
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.embeddings import Embedder, HashingEmbedder
from insightforge_agent.pipeline.errors import RunFailed
from insightforge_agent.stores.source_store import SourceStore


@dataclass
class Deps:
    repos: Any  # MemoryRepos or SqliteRepos
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
    recursion_limit: int = 60

    _spent: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)

    def spend(self, owner_id: str, run_id: str, *texts: str) -> None:
        """Count the estimated input tokens of one model call against the Run's token cap."""
        key = (owner_id, run_id)
        self._spent[key] = self._spent.get(key, 0) + sum(estimate_tokens(t) for t in texts)
        if self.run_token_cap is not None and self._spent[key] > self.run_token_cap:
            raise RunFailed(f"the Run's token cap of {self.run_token_cap} was exceeded")

    def log(self, owner_id: str, run_id: str, type_: str, **payload) -> None:
        self.repos.events.append(RunEvent(
            owner_id=owner_id, run_id=run_id, type=type_, payload=payload, created_at=self.now(),
        ))
