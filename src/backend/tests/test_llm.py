from langchain_core.language_models.fake_chat_models import FakeListChatModel

from insightforge_agent.config import Settings
from insightforge_agent.llm import build_chat_model


def fake_settings(monkeypatch) -> Settings:
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("SEARCH_PROVIDERS", "fake")
    return Settings()


def test_fake_provider_returns_fake_model_for_each_role(monkeypatch):
    s = fake_settings(monkeypatch)
    for role in ("planner", "writer", "light"):
        assert isinstance(build_chat_model(role, s), FakeListChatModel)


def test_gemini_role_uses_configured_model_name(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("GEMINI_LIGHT_MODEL", "gemini-test-light")
    model = build_chat_model("light", Settings())
    assert type(model).__name__ == "ChatGoogleGenerativeAI"
    assert model.model.endswith("gemini-test-light")


def test_anthropic_and_groq_build(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert type(build_chat_model("planner", Settings())).__name__ == "ChatAnthropic"
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert type(build_chat_model("writer", Settings())).__name__ == "ChatGroq"


def test_anthropic_writer_has_room_for_a_whole_report(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert build_chat_model("writer", Settings()).max_tokens >= 16000
