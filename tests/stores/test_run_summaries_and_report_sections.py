"""T8 (#9): the Run summary repository (in memory and SQLite) and the `report_sections` index."""

from datetime import UTC, date, datetime, timedelta

import pytest
from qdrant_client import QdrantClient

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.memory import MemoryRef
from insightforge_agent.domain.models import RunSummary
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.stores.report_sections import (
    COLLECTION,
    ReportSectionIndex,
    SectionEntry,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def summary(rid, owner="alice", item=None, at=T0, brief="b") -> RunSummary:
    return RunSummary(owner_id=owner, run_id=rid, watchlist_item_id=item, created_at=at,
                      brief=brief, themes=["Sales"], key_entities=["BYD"], gaps=["g"],
                      memory_indexed=True)


# --- Run summaries ---------------------------------------------------------------------------

def test_a_summary_is_stored_once_per_run(repos):
    first = repos.summaries.add(summary("r1", brief="first"))
    again = repos.summaries.add(summary("r1", brief="second"))
    assert first == again == repos.summaries.get_for_run("alice", "r1")
    assert again.brief == "first"


def test_summaries_are_owner_scoped(repos):
    repos.summaries.add(summary("r1"))
    with pytest.raises(NotFoundError):
        repos.summaries.get_for_run("bob", "r1")
    assert repos.summaries.list_for("bob", None) == []


def test_summaries_are_listed_newest_first_per_watchlist_item_or_one_shot(repos):
    for n in range(3):
        repos.summaries.add(summary(f"one{n}", at=T0 + timedelta(days=n)))
        repos.summaries.add(summary(f"w{n}", item="w1", at=T0 + timedelta(days=n)))
    repos.summaries.add(summary("other", item="w2"))
    repos.summaries.add(summary("bobs", owner="bob"))
    assert [s.run_id for s in repos.summaries.list_for("alice", None)] == ["one2", "one1", "one0"]
    assert [s.run_id for s in repos.summaries.list_for("alice", "w1")] == ["w2", "w1", "w0"]
    assert [s.run_id for s in repos.summaries.list_for("alice", "w2")] == ["other"]


def test_sqlite_summaries_survive_a_reopen(tmp_path):
    from insightforge_agent.stores.sqlite import SqliteRepos

    path = str(tmp_path / "s.db")
    first = SqliteRepos(path)
    first.summaries.add(summary("r1"))
    first.close()
    assert SqliteRepos(path).summaries.get_for_run("alice", "r1") == summary("r1")


# --- report_sections ---------------------------------------------------------------------------

ENTRIES = [SectionEntry(0, 0, "Sales", "BYD sold 4.27 million vehicles in 2025."),
           SectionEntry(0, 1, "Sales", "Tesla sold 1.80 million vehicles in 2025."),
           SectionEntry(1, 0, "Pricing", "BYD cut prices in China in 2025.")]


@pytest.fixture
def client():
    return QdrantClient(":memory:")


def index_of(client, model="test-hash"):
    return ReportSectionIndex(client, HashingEmbedder(), model, 256)


def test_search_returns_references_and_metadata_never_text(client):
    index = index_of(client)
    assert index.index("alice", "rep_a", "run_a", date(2026, 9, 1), ENTRIES) == 3
    (hit, *_) = index.search("alice", ["rep_a"], "BYD vehicles sold 2025", k=2)
    assert hit.ref == MemoryRef("rep_a", 0, 0) and hit.run_id == "run_a"
    assert hit.heading == "Sales" and hit.report_date == "2026-09-01"
    payloads = [p.payload for p in client.scroll(COLLECTION, limit=10)[0]]
    assert all("text" not in p and p["embedding_model"] == "test-hash" for p in payloads)
    assert {(p["report_id"], p["run_id"], p["section_index"], p["claim_index"])
            for p in payloads} == {("rep_a", "run_a", 0, 0), ("rep_a", "run_a", 0, 1),
                                   ("rep_a", "run_a", 1, 0)}


def test_search_is_filtered_by_owner(client):
    index = index_of(client)
    index.index("alice", "rep_a", "run_a", date(2026, 9, 1), ENTRIES)
    assert index.search("bob", ["rep_a"], "BYD vehicles sold 2025") == []


def test_search_is_limited_to_the_explicit_report_set(client):
    index = index_of(client)
    index.index("alice", "rep_a", "run_a", date(2026, 9, 1), ENTRIES)
    index.index("alice", "rep_b", "run_b", date(2026, 9, 2), ENTRIES)
    assert {h.ref.report_id for h in index.search("alice", ["rep_b"], "BYD", k=10)} == {"rep_b"}
    assert index.search("alice", [], "BYD") == []  # nothing eligible: nothing searched


def test_search_is_filtered_by_embedding_model(client):
    index_of(client, "model-a").index("alice", "rep_a", "run_a", date(2026, 9, 1), ENTRIES)
    assert index_of(client, "model-b").search("alice", ["rep_a"], "BYD") == []


def test_indexing_again_is_idempotent(client):
    index = index_of(client)
    for _ in range(2):
        index.index("alice", "rep_a", "run_a", date(2026, 9, 1), ENTRIES)
    assert client.count(COLLECTION).count == 3


def test_a_collection_of_another_vector_size_is_refused(client):
    ReportSectionIndex(client, HashingEmbedder(64), "small", 64).index(
        "alice", "rep_a", "run_a", date(2026, 9, 1), ENTRIES)
    with pytest.raises(RuntimeError, match="64-dimensional"):
        index_of(client).search("alice", ["rep_a"], "BYD")
