from pathlib import Path

import pytest

from tests import network_guard

_ENV_KEYS = (
    "LLM_PROVIDER", "SEARCH_PROVIDERS", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GROQ_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY", "BRAVE_API_KEY",
)

# Test tiers (docs/benchmark/methodology.md, "CI tiers"). Files that name their tier with a
# `pytestmark` keep it; the suites below are labelled here so one line says where they sit.
_HERE = Path(__file__).parent
_TIER_OF = {
    "scripted": [_HERE / "pipeline", _HERE / "test_planner_spike_scripted.py",
                 _HERE / "test_llm.py"],
    "replay": [_HERE / "benchmark" / "test_judge_and_recording.py",
               _HERE / "benchmark" / "test_report_and_cli.py",
               _HERE / "benchmark" / "test_integrity_corrections.py"],
}


def pytest_collection_modifyitems(items):
    for item in items:
        path = Path(str(item.path))
        for tier, places in _TIER_OF.items():
            if any(path == p or p in path.parents for p in places):
                item.add_marker(getattr(pytest.mark, tier))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate Settings from the developer's shell and .env file."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def network_guard_applies(node) -> bool:
    return node.get_closest_marker("live") is None


@pytest.fixture(autouse=True)
def no_outbound_network(request, monkeypatch):
    """Every test except a `live` one fails on an outbound connection, even a swallowed one."""
    if not network_guard_applies(request.node):
        yield
        return
    attempts: list[str] = []
    network_guard.install(monkeypatch, attempts)
    yield
    if attempts:
        pytest.fail("outbound network use in an offline test: " + "; ".join(attempts), pytrace=False)
