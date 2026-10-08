import pytest

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.ids import derive_observation_id, derive_source_id
from insightforge_agent.domain.passages import split_paragraphs
from insightforge_agent.stores import source_store as source_store_module
from insightforge_agent.stores.source_store import PassageDraft, SourceStore
from tests.stores.builders import T0

BODY = "First paragraph.\n\nSecond paragraph.\n\n\n\nThird paragraph."
URL = "https://example.com/ev-report"


@pytest.fixture
def store(repos):
    return SourceStore(repos.sources, repos.observations, repos.passages)


def ingest(store, owner="alice", run_id="r1", body=BODY, at=T0, url=URL):
    return store.ingest_web(owner_id=owner, run_id=run_id, url=url, title="EV report",
                            body=body, fetched_at=at)


def test_split_paragraphs_drops_blanks_and_normalises_newlines():
    assert split_paragraphs("a\r\n\r\nb\n\n\n\n  \n\nc ") == ["a", "b", "c"]
    assert split_paragraphs("") == []


def test_source_id_is_derived_from_kind_and_locator():
    assert derive_source_id("web", URL) == derive_source_id("web", URL)
    assert derive_source_id("web", URL) != derive_source_id("web", URL + "?x=1")
    assert derive_source_id("web", "abc") != derive_source_id("pdf", "abc")


def test_observation_id_is_derived_from_the_fetch():
    a = derive_observation_id("alice", "src_1", T0, "body")
    assert a == derive_observation_id("alice", "src_1", T0, "body")
    assert a != derive_observation_id("alice", "src_1", T0, "other body")
    assert a != derive_observation_id("bob", "src_1", T0, "body")


def test_ingest_web_splits_into_addressable_passages(store):
    obs = ingest(store)
    passages = store.read_source("alice", obs.source_id).passages
    assert [(p.index, p.text) for p in passages] == [
        (0, "First paragraph."), (1, "Second paragraph."), (2, "Third paragraph."),
    ]
    assert all(p.observation_id == obs.id for p in passages)


def test_read_source_resolves_a_content_derived_id(store):
    obs = ingest(store)
    assert obs.source_id == derive_source_id("web", URL)
    contents = store.read_source("alice", derive_source_id("web", URL))
    assert contents.source.title == "EV report"
    assert contents.observation == obs


def test_reingesting_the_same_fetch_keeps_the_original_split(store, monkeypatch):
    first = ingest(store)
    # Parsing changes later; the stored Observation must not be re-split.
    monkeypatch.setattr(source_store_module, "split_paragraphs", lambda body: [body])
    again = ingest(store)
    assert again.id == first.id
    texts = [p.text for p in store.read_source("alice", first.source_id).passages]
    assert texts == ["First paragraph.", "Second paragraph.", "Third paragraph."]


def test_a_later_fetch_is_a_new_observation_with_its_own_passages(store):
    from datetime import timedelta

    old = ingest(store)
    new = ingest(store, body="Only one paragraph now.", at=T0 + timedelta(days=7))
    assert new.id != old.id and new.source_id == old.source_id
    latest = store.read_source("alice", old.source_id)
    assert latest.observation == new
    assert [p.text for p in latest.passages] == ["Only one paragraph now."]
    # The original Observation's Passages are untouched, so old citations still resolve.
    assert store.get_passages("alice", old.id, [0, 2])[1].text == "Third paragraph."


def test_ingest_document_keeps_page_and_heading_and_hashes_the_file(store):
    obs = store.ingest_document(
        owner_id="alice", run_id="r1", content_hash="deadbeef", filename="report.pdf",
        chunks=[PassageDraft("Intro text", page=1, section_heading="Overview"),
                PassageDraft("Numbers", page=4, section_heading="Results")],
        fetched_at=T0,
    )
    assert obs.source_id == derive_source_id("pdf", "deadbeef")
    contents = store.read_source("alice", obs.source_id)
    assert contents.source.kind == "pdf" and contents.source.title == "report.pdf"
    assert [(p.page, p.section_heading) for p in contents.passages] == [(1, "Overview"), (4, "Results")]


def test_get_passages_returns_a_batch_in_the_order_asked(store):
    obs = ingest(store)
    got = store.get_passages("alice", obs.id, [2, 0])
    assert [p.text for p in got] == ["Third paragraph.", "First paragraph."]


def test_unfetched_or_unknown_source(store, repos):
    with pytest.raises(NotFoundError):
        store.read_source("alice", "src_missing")


def test_another_user_cannot_resolve_a_source(store):
    obs = ingest(store, owner="alice")
    with pytest.raises(NotFoundError):
        store.read_source("bob", obs.source_id)
    with pytest.raises(NotFoundError):
        store.get_passages("bob", obs.id, [0])


def test_two_users_fetching_the_same_url_get_separate_records(store):
    a = ingest(store, owner="alice", body="Alice copy.")
    b = ingest(store, owner="bob", body="Bob copy.")
    assert a.source_id == b.source_id and a.id != b.id
    assert store.read_source("alice", a.source_id).passages[0].text == "Alice copy."
    assert store.read_source("bob", b.source_id).passages[0].text == "Bob copy."
