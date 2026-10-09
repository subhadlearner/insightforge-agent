"""Builds a Run's dependencies from Settings: real models, search and fetching."""

from typing import Any

from insightforge_agent.agents.web import HttpFetcher, build_search
from insightforge_agent.config import Settings
from insightforge_agent.llm import build_chat_model
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.stores.source_store import SourceStore


def build_deps(settings: Settings, repos: Any, checkpointer: Any) -> Deps:
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
    )
