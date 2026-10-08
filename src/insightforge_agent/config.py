"""Typed application settings (design.md §10)."""

import logging
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

LLMProvider = Literal["anthropic", "gemini", "groq", "fake"]
SearchProvider = Literal["tavily", "serpapi", "brave", "fake"]

_LLM_SECRET_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Models. "fake" is for tests only and needs no secret.
    llm_provider: LLMProvider = "gemini"
    anthropic_planner_model: str = "claude-haiku-5-5"
    anthropic_writer_model: str = "claude-haiku-5-5"
    anthropic_light_model: str = "claude-haiku-5-5"
    gemini_planner_model: str = "gemini-3.5-flash"
    gemini_writer_model: str = "gemini-3.5-flash"
    gemini_light_model: str = "gemini-3.5-flash-lite"
    groq_planner_model: str = "openai/gpt-oss-120b"
    groq_writer_model: str = "openai/gpt-oss-120b"
    groq_light_model: str = "openai/gpt-oss-20b"

    # Embeddings: local in every environment, independent of the chat provider.
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # Search: ordered; a provider that fails falls through to the next.
    search_providers: Annotated[list[SearchProvider], NoDecode] = ["tavily", "serpapi"]

    # Secrets.
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    groq_api_key: str | None = None
    tavily_api_key: str | None = None
    serpapi_api_key: str | None = None
    brave_api_key: str | None = None

    # Infrastructure.
    qdrant_url: str = "http://localhost:6335"
    database_url: str = "sqlite:///insightforge.db"
    personas_file: str = "users.yml"

    @field_validator("search_providers", mode="before")
    @classmethod
    def _split_providers(cls, v):
        if isinstance(v, str):
            return [p.strip().lower() for p in v.split(",") if p.strip()]
        return v

    def _search_key(self, provider: str) -> str | None:
        return getattr(self, f"{provider}_api_key", None)

    @property
    def usable_search_providers(self) -> list[str]:
        """Listed search providers that have credentials, in order."""
        return [
            p for p in self.search_providers
            if p == "fake" or self._search_key(p)
        ]

    @model_validator(mode="after")
    def _validate_startup(self) -> "Settings":
        secret_env = _LLM_SECRET_ENV.get(self.llm_provider)
        if secret_env and not getattr(self, secret_env.lower()):
            raise ValueError(
                f"{secret_env} is required when LLM_PROVIDER={self.llm_provider}"
            )
        usable = self.usable_search_providers
        for p in self.search_providers:
            if p not in usable:
                logger.warning("Search provider %r skipped: no credentials", p)
        if not usable:
            raise ValueError(
                "No usable search provider: set a key for at least one of "
                f"SEARCH_PROVIDERS={','.join(self.search_providers)}"
            )
        return self
