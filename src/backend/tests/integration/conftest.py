"""Integration tests: the real Docling, the real fastembed model and in-memory Qdrant.

Offline like every non-live test (the network guard stays on). The model files must already be
in the local cache: run `uv run insightforge-agent models download` once. Without them the
tests are skipped, never run against the network. Excluded from the default run by addopts;
select them with `uv run pytest -m integration`."""

from pathlib import Path

import pytest

from tests.pipeline.conftest import make_deps  # noqa: F401 - shared fixture

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def offline_models(monkeypatch):
    """Model files come from the local cache only. huggingface_hub reads HF_HUB_OFFLINE when
    it is imported, so the already-loaded constant is patched as well."""
    import huggingface_hub.constants

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_OFFLINE", True)


_CACHE: dict = {}


def _cached(name, build):
    if name not in _CACHE:
        try:
            _CACHE[name] = build()
        except Exception as e:  # noqa: BLE001 - a missing model file has no single error type
            _CACHE[name] = e
    if isinstance(_CACHE[name], Exception):
        pytest.skip(f"{name} is not in the local cache "
                    f"(run `uv run insightforge-agent models download`): {_CACHE[name]}")
    return _CACHE[name]


@pytest.fixture
def embedder(offline_models):
    from insightforge_agent.embeddings import FastEmbedEmbedder

    def build():
        e = FastEmbedEmbedder("BAAI/bge-small-en-v1.5")
        e.embed(["load"])
        return e
    return _cached("fastembed BAAI/bge-small-en-v1.5", build)


@pytest.fixture
def chunker(offline_models):
    from insightforge_agent.agents.doc_reader import DoclingChunker
    from insightforge_agent.pdf_writer import make_pdf

    def build():
        c = DoclingChunker()
        c.chunk("load.pdf", make_pdf([[(16, "Load"), (10, "Loads the layout model.")]]))
        return c
    return _cached("Docling layout model", build)
