"""T7: the `doc_chunks` collection over in-memory Qdrant (no server, no network)."""

from datetime import UTC, datetime

import pytest
from qdrant_client import QdrantClient

from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.stores.doc_chunks import COLLECTION, DocChunkIndex
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.source_store import PassageDraft, SourceStore

T0 = datetime(2026, 10, 1, tzinfo=UTC)
TOPICS = ["revenue grew to 4.2 billion dollars", "the board approved a dividend",
          "battery cell costs fell sharply", "headcount reached 12,000 employees",
          "a new plant opened in Hungary", "the company entered the Brazilian market",
          "research spending rose to 900 million", "net margin improved to 6 percent",
          "solid state battery pilot line started", "share buyback of 500 million announced"]


def index_of(client=None, model="test-hash", dims=256):
    return DocChunkIndex(client or QdrantClient(":memory:"), HashingEmbedder(dims), model, dims)


def ingest(store, index, owner, run, name="annual.pdf", texts=TOPICS, data=b"pdf"):
    obs = store.ingest_document(owner_id=owner, run_id=run, content_hash=f"h-{name}-{data!r}",
                                filename=name, fetched_at=T0, credibility_score=0.7,
                                chunks=[PassageDraft(t, page=n + 1, section_heading="Results")
                                        for n, t in enumerate(texts)])
    source = store.read_source(owner, obs.source_id).source
    passages = store._passages.list(owner, obs.id)
    index.index(owner, run, source, passages)
    return obs


@pytest.fixture
def world():
    repos = MemoryRepos()
    return SourceStore(repos.sources, repos.observations, repos.passages), index_of()


def test_search_returns_at_most_k_eight_ranked_chunk_ids_and_never_text(world):
    store, index = world
    obs = ingest(store, index, "alice", "r1")
    hits = index.search("alice", "r1", "revenue grew to billion dollars")
    assert len(hits) == 8
    assert hits[0].passage.observation_id == obs.id and hits[0].passage.index == 0
    assert hits[0].page == 1 and hits[0].source_filename == "annual.pdf"
    assert [h.relevance_score for h in hits] == sorted((h.relevance_score for h in hits), reverse=True)
    assert all(not hasattr(h, "text") for h in hits)
    assert all(t not in repr(hits) for t in TOPICS)


def test_a_run_searches_only_its_own_uploads(world):
    store, index = world
    ingest(store, index, "alice", "r1", name="a.pdf")
    ingest(store, index, "alice", "r2", name="b.pdf", texts=["revenue grew in run two"], data=b"b")
    assert {h.source_filename for h in index.search("alice", "r2", "revenue")} == {"b.pdf"}
    assert {h.source_filename for h in index.search("alice", "r1", "revenue")} == {"a.pdf"}


def test_queries_are_filtered_by_owner(world):
    store, index = world
    ingest(store, index, "alice", "r1")
    assert index.search("bob", "r1", "revenue") == []


def test_indexing_is_idempotent(world):
    store, index = world
    obs = ingest(store, index, "alice", "r1")
    index.index("alice", "r1", store.read_source("alice", obs.source_id).source,
                store._passages.list("alice", obs.id))
    assert index._client.count(COLLECTION).count == len(TOPICS)


def test_payload_carries_the_required_metadata_and_the_model_name(world):
    store, index = world
    ingest(store, index, "alice", "r1")
    (point,), _ = index._client.scroll(COLLECTION, limit=1)
    assert {"run_id", "owner_id", "source_filename", "page", "section_heading",
            "observation_id", "passage_index", "embedding_model"} <= set(point.payload)
    assert point.payload["embedding_model"] == "test-hash"


def test_chunks_from_another_embedding_model_are_never_returned(world):
    store, index = world
    ingest(store, index, "alice", "r1")
    other = DocChunkIndex(index._client, HashingEmbedder(256), "other-model", 256)
    assert other.search("alice", "r1", "revenue") == []


def test_a_collection_with_another_vector_size_is_refused():
    client = QdrantClient(":memory:")
    index_of(client).search("alice", "r1", "x")
    with pytest.raises(RuntimeError, match="384"):
        index_of(client, model="bge", dims=384).search("alice", "r1", "x")
