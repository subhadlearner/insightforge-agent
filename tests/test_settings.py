import pytest
from pydantic import ValidationError

from insightforge_agent.config import Settings


def make(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_loads_from_environment(monkeypatch):
    s = make(monkeypatch, LLM_PROVIDER="gemini", GOOGLE_API_KEY="g", TAVILY_API_KEY="t")
    assert s.llm_provider == "gemini"
    assert s.search_providers == ["tavily", "serpapi"]


def test_pdf_web_origin_defaults_to_next_dev_host(monkeypatch):
    monkeypatch.delenv("WEB_ORIGIN", raising=False)
    settings = Settings(llm_provider="fake", search_providers=["fake"], _env_file=None)
    assert settings.web_origin == "http://localhost:3000"
    assert Settings.model_fields["web_origin"].default == "http://localhost:3000"


def test_loads_from_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("LLM_PROVIDER=groq\nGROQ_API_KEY=k\nTAVILY_API_KEY=t\n")
    assert Settings().llm_provider == "groq"


def test_missing_llm_secret_refuses_startup(monkeypatch):
    with pytest.raises(ValidationError, match="GOOGLE_API_KEY"):
        make(monkeypatch, LLM_PROVIDER="gemini", TAVILY_API_KEY="t")


def test_no_usable_search_provider_refuses_startup(monkeypatch):
    with pytest.raises(ValidationError, match="search provider"):
        make(monkeypatch, LLM_PROVIDER="gemini", GOOGLE_API_KEY="g")


def test_missing_fallback_key_does_not_stop_app(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        s = make(monkeypatch, LLM_PROVIDER="gemini", GOOGLE_API_KEY="g",
                 TAVILY_API_KEY="t", SEARCH_PROVIDERS="tavily,serpapi")
    assert s.usable_search_providers == ["tavily"]
    assert "serpapi" in caplog.text


def test_usable_provider_may_be_a_later_one(monkeypatch):
    s = make(monkeypatch, LLM_PROVIDER="gemini", GOOGLE_API_KEY="g", SERPAPI_API_KEY="s")
    assert s.usable_search_providers == ["serpapi"]


def test_search_providers_comma_separated(monkeypatch):
    s = make(monkeypatch, LLM_PROVIDER="gemini", GOOGLE_API_KEY="g",
             BRAVE_API_KEY="b", SEARCH_PROVIDERS="brave")
    assert s.search_providers == ["brave"]


def test_fake_provider_needs_no_keys(monkeypatch):
    s = make(monkeypatch, LLM_PROVIDER="fake", SEARCH_PROVIDERS="fake")
    assert s.usable_search_providers == ["fake"]


def test_unknown_search_provider_is_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        make(monkeypatch, LLM_PROVIDER="fake", SEARCH_PROVIDERS="tavilly")


def test_evidence_limits_and_bucket_thresholds_are_settings_that_reach_the_pipeline(monkeypatch):
    from insightforge_agent.pipeline.wiring import build_deps
    from insightforge_agent.stores.memory import MemoryRepos

    s = make(monkeypatch, LLM_PROVIDER="fake", TAVILY_API_KEY="t", EVIDENCE_BUDGET_TOKENS="4000",
             PASSAGE_TOKEN_CAP="1500", CREDIBILITY_HIGH="0.8", RECENCY_FRESH_DAYS="90",
             RUN_TOKEN_CAP="50000")
    deps = build_deps(s, MemoryRepos(), checkpointer=None)
    assert (deps.evidence_budget_tokens, deps.passage_token_cap, deps.run_token_cap) == (
        4000, 1500, 50000)
    assert (deps.thresholds.credibility_high, deps.thresholds.recency_fresh_days) == (0.8, 90)
    assert Settings.model_fields["evidence_budget_tokens"].default == 6000


def test_drafting_reliability_settings_reach_the_pipeline(monkeypatch):
    """T18 CS3: native structured drafting is on by default and can be switched off; the
    drafting Passage cap is off by default."""
    from insightforge_agent.llm import native_json
    from insightforge_agent.pipeline.wiring import build_deps
    from insightforge_agent.stores.memory import MemoryRepos

    deps = build_deps(make(monkeypatch, LLM_PROVIDER="fake", TAVILY_API_KEY="t"), MemoryRepos(),
                      checkpointer=None)
    assert (deps.native_json, deps.draft_passage_cap) == (native_json, None)
    deps = build_deps(make(monkeypatch, LLM_PROVIDER="fake", TAVILY_API_KEY="t",
                           DRAFT_STRUCTURED_OUTPUT="false", DRAFT_PASSAGE_CAP="150"),
                      MemoryRepos(), checkpointer=None)
    assert (deps.native_json, deps.draft_passage_cap) == (None, 150)


def test_the_counterpart_fallback_is_on_by_default_and_can_be_switched_off(monkeypatch):
    from insightforge_agent.pipeline.wiring import build_deps
    from insightforge_agent.stores.memory import MemoryRepos

    on = build_deps(make(monkeypatch, LLM_PROVIDER="fake", TAVILY_API_KEY="t"), MemoryRepos(),
                    checkpointer=None)
    off = build_deps(make(monkeypatch, LLM_PROVIDER="fake", TAVILY_API_KEY="t",
                          FACT_CHECK_COUNTERPART="false"), MemoryRepos(), checkpointer=None)
    assert (on.fact_check_counterpart, off.fact_check_counterpart) == (True, False)


def test_documents_are_wired_with_the_configured_local_embedding_model(monkeypatch):
    """T7: production wiring builds the Doc Reader's pieces without loading a model or opening a
    connection; the one embedding model serves documents, relevance and the Fact-Checker."""
    from insightforge_agent.agents.doc_reader import DoclingChunker
    from insightforge_agent.embeddings import FastEmbedEmbedder
    from insightforge_agent.pipeline.graph import runnable_source_types
    from insightforge_agent.pipeline.wiring import build_deps
    from insightforge_agent.stores.memory import MemoryRepos

    s = make(monkeypatch, LLM_PROVIDER="fake", TAVILY_API_KEY="t")
    deps = build_deps(s, MemoryRepos(), checkpointer=None)
    assert isinstance(deps.embedder, FastEmbedEmbedder)
    assert deps.embedder.model_name == "BAAI/bge-small-en-v1.5"
    assert deps.doc_index is not None and deps.doc_index._embedder is deps.embedder
    assert deps.doc_index._dimensions == 384
    assert isinstance(deps.chunker, DoclingChunker)
    # T8: report_sections uses the same model and the same Qdrant client as doc_chunks.
    assert deps.report_index is not None and deps.report_index._embedder is deps.embedder
    assert deps.report_index._client is deps.doc_index._client
    assert runnable_source_types(deps) == ["web", "documents", "memory"]
