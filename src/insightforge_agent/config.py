"""Typed application settings (design.md §10)."""

import logging
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # Evidence: budget, per-call Passage cap, bucket thresholds (design.md sections 4, 5, 10).
    evidence_budget_tokens: int = 6000
    passage_token_cap: int = 3000
    run_token_cap: int | None = None  # None: no cap on the model tokens a Run may spend
    fact_check_rate: float = 0.2
    fact_check_minimum: int = 5
    similarity_threshold: float = 0.85
    credibility_high: float = 0.65
    credibility_medium: float = 0.4
    recency_fresh_days: int = 365
    recency_recent_days: int = 1095

    # Clarification: how long a Run waits for the answer to its one question.
    clarification_timeout_minutes: int = 30

    # T15 public Report links. A dedicated secret may be supplied; when omitted, the demo
    # deliberately reuses SESSION_SECRET (the persona cookie has its own purpose prefix).
    share_jwt_secret: SecretStr | None = None
    share_link_ttl_days: int = Field(default=7, gt=0)
    web_origin: str = "http://localhost:3000"

    # Diff (T12): consecutive Not reconfirmed Diffs after which a Finding shows as unconfirmed.
    not_reconfirmed_threshold: int = Field(default=3, ge=1)

    # Retention (T13): days after a terminal Run (its Report, or its failure) before its
    # unreferenced raw material may be removed by the daily sweep.
    retention_days: int = Field(default=30, gt=0)

    # Runtime estimate (T14, design.md section 10): seconds shown before submit, as
    # base + PDFs x pdf_ingest + one cost per enabled source type. A heuristic, not a promise.
    runtime_estimate_base_seconds: int = Field(default=30, ge=0)
    runtime_estimate_pdf_ingest_seconds: int = Field(default=15, ge=0)
    runtime_estimate_web_seconds: int = Field(default=45, ge=0)
    runtime_estimate_documents_seconds: int = Field(default=30, ge=0)
    runtime_estimate_memory_seconds: int = Field(default=15, ge=0)

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
    checkpoint_database_url: str = "sqlite:///insightforge-checkpoints.db"  # LangGraph threads
    personas_file: str = "users.yml"

    # API (design.md section 9). The session secret signs the persona cookie; the app refuses
    # to start without one. Persona selection, not security.
    session_secret: SecretStr | None = None
    session_cookie_secure: bool = False  # True when served over HTTPS
    run_concurrency: int = Field(default=2, ge=1)  # Runs executing at once (RunExecutor)
    run_queue_limit: int = Field(default=2, ge=0)  # new Runs that may wait for a free worker
    sse_poll_seconds: float = Field(default=0.5, gt=0)  # how often the SSE stream tails run_events
    sse_max_stream_seconds: float = Field(default=60, gt=0)  # then the client reconnects

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
