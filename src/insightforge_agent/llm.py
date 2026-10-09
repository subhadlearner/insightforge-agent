"""The only module that knows about chat model providers (design.md §10)."""

from typing import Literal

from langchain_core.language_models import BaseChatModel

from insightforge_agent.config import Settings

Role = Literal["planner", "writer", "light"]


def build_chat_model(role: Role, settings: Settings | None = None) -> BaseChatModel:
    """Build the chat model for a role. `light` is the cheaper model used for
    Researcher summaries and entailment calls."""
    s = settings or Settings()
    provider = s.llm_provider
    if provider == "fake":
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        return FakeListChatModel(responses=["ok"])

    name = getattr(s, f"{provider}_{role}_model")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        extra = {}
        if role == "planner":
            # The Planner thread is resumed, and deepagents rewrites earlier messages; signed
            # thinking blocks then fail with "Invalid signature" (design.md section 12).
            extra["thinking"] = {"type": "disabled"}
        return ChatAnthropic(model=name, api_key=s.anthropic_api_key, **extra)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=name, google_api_key=s.google_api_key)
    from langchain_groq import ChatGroq

    return ChatGroq(model=name, api_key=s.groq_api_key)
