import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.source_store import SourceStore
from tests.pipeline import fakes


@pytest.fixture
def make_deps():
    with SqliteSaver.from_conn_string(":memory:") as saver:
        def make(**overrides) -> Deps:
            repos = MemoryRepos()
            search = fakes.FixtureSearch()
            parts = dict(
                repos=repos,
                store=SourceStore(repos.sources, repos.observations, repos.passages),
                planner_model=fakes.planner(), writer_model=fakes.writer(),
                light_model=fakes.light(), researcher_model=fakes.researcher(),
                search=search, fetcher=search, checkpointer=saver,
            )
            parts.update(overrides)
            return Deps(**parts)
        yield make
