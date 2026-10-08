import pytest

_ENV_KEYS = (
    "LLM_PROVIDER", "SEARCH_PROVIDERS", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GROQ_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY", "BRAVE_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate Settings from the developer's shell and .env file."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
