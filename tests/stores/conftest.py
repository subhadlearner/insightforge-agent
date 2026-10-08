import pytest

from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.sqlite import SqliteRepos


@pytest.fixture(params=["memory", "sqlite"])
def repos(request, tmp_path):
    """Every repository contract runs against the fakes and against SQLite."""
    if request.param == "memory":
        return MemoryRepos()
    return SqliteRepos(str(tmp_path / "test.db"))
