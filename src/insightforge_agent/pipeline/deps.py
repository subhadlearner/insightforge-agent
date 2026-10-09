"""Everything a Run needs from outside, passed in so tests can supply scripted fakes."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel

from insightforge_agent.agents.researcher import SummaryBook
from insightforge_agent.agents.web import PageFetcher, SearchProvider
from insightforge_agent.domain.models import RunEvent
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
    high_credibility: float = 0.65
    writer_retries: int = 2
    recursion_limit: int = 60
    summaries: SummaryBook = field(default_factory=SummaryBook)

    def log(self, owner_id: str, run_id: str, type_: str, **payload) -> None:
        self.repos.events.append(RunEvent(
            owner_id=owner_id, run_id=run_id, type=type_, payload=payload, created_at=self.now(),
        ))
