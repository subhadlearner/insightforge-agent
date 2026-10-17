"""The only module that knows about chat model providers (design.md §10)."""

from typing import Literal, get_args

from langchain_core.language_models import BaseChatModel

from insightforge_agent.config import LLMProvider, Settings

Role = Literal["planner", "writer", "light"]
ROLES: tuple[Role, ...] = get_args(Role)
# The real chat providers; "fake" is for tests only.
CHAT_PROVIDERS: tuple[str, ...] = tuple(p for p in get_args(LLMProvider) if p != "fake")


def configured_model_name(role: Role, settings: Settings) -> str:
    """The model id `build_chat_model` builds for a role under these Settings."""
    if settings.llm_provider == "fake":
        return "fake"
    return getattr(settings, f"{settings.llm_provider}_{role}_model")


def build_chat_model(role: Role, settings: Settings | None = None) -> BaseChatModel:
    """Build the chat model for a role. `light` is the cheaper model used for
    Researcher summaries and entailment calls."""
    s = settings or Settings()
    provider = s.llm_provider
    if provider == "fake":
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        return FakeListChatModel(responses=["ok"])

    name = configured_model_name(role, s)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        extra = {}
        if role == "planner":
            # The Planner thread is resumed, and deepagents rewrites earlier messages; signed
            # thinking blocks then fail with "Invalid signature" (design.md section 12).
            extra["thinking"] = {"type": "disabled"}
        if role == "writer":
            # The Report is one JSON reply; the 4096 default cuts a long one off mid-string.
            extra["max_tokens"] = 16000
        return ChatAnthropic(model=name, api_key=s.anthropic_api_key, **extra)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=name, google_api_key=s.google_api_key)
    from langchain_groq import ChatGroq

    return ChatGroq(model=name, api_key=s.groq_api_key)
