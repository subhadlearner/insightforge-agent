"""Second review round: atomic Observation writes, durable empty splits, duplicate Reports,
and SQLite durability across a close and reopen."""

from datetime import timedelta

import pytest

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.stores import source_store as module
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.stores.builders import T0, event, passage, report, run

BODY = "One.\n\nTwo.\n\nThree."
KWARGS = dict(owner_id="alice", run_id="r1", url="https://example.com/p", title="P",
              body=BODY, fetched_at=T0)


class Boom(RuntimeError):
    pass


class FailingPassages:
    """Wraps a PassageRepository and fails add_all, as a crash during Passage storage would."""

    def __init__(self, inner):
        self._inner = inner

    def add_all(self, *args, **kwargs):
        raise Boom("crash while storing passages")

    def __getattr__(self, name):
        return getattr(self._inner, name)


class FailingObservations:
    """Wraps an ObservationRepository and fails add, as a crash after Passages were stored."""

    def __init__(self, inner):
        self._inner = inner

    def add(self, observation):
        raise Boom("crash while storing the observation")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def store_over(repos, passages=None, observations=None):
    return SourceStore(repos.sources, observations or repos.observations,
                       passages or repos.passages)


def stored_texts(repos, observation_id):
    return [p.text for p in repos.passages.list("alice", observation_id)]


# 1. Observation + Passage writes are atomic -------------------------------------------------

def test_crash_while_storing_passages_leaves_no_incomplete_observation(repos):
    with pytest.raises(Boom):
        store_over(repos, passages=FailingPassages(repos.passages)).ingest_web(**KWARGS)
    source_id = repos.sources.list_for_run("alice", "r1")[0].id
    assert repos.observations.list_for_source("alice", source_id) == []

    # Retry on healthy repositories produces the full Observation.
    obs = store_over(repos).ingest_web(**KWARGS)
    assert stored_texts(repos, obs.id) == ["One.", "Two.", "Three."]


def test_crash_after_passages_but_before_the_observation_recovers_with_the_same_split(
    repos, monkeypatch
):
    with pytest.raises(Boom):
        store_over(repos, observations=FailingObservations(repos.observations)).ingest_web(**KWARGS)
    source_id = repos.sources.list_for_run("alice", "r1")[0].id
    assert repos.observations.list_for_source("alice", source_id) == []

    # Parsing changed in the meantime: the split that was already stored still wins.
    monkeypatch.setattr(module, "split_paragraphs", lambda body: ["changed parser"])
    obs = store_over(repos).ingest_web(**KWARGS)
    assert stored_texts(repos, obs.id) == ["One.", "Two.", "Three."]
    assert repos.observations.get("alice", obs.id) == obs


def test_an_observation_that_exists_always_has_its_passages(repos):
    obs = store_over(repos).ingest_web(**KWARGS)
    assert repos.observations.get("alice", obs.id)
    assert len(stored_texts(repos, obs.id)) == 3


# 2. A zero-Passage split is durable ----------------------------------------------------------

def test_an_empty_split_is_recorded_so_add_all_cannot_split_it_later(repos):
    repos.passages.add_all("alice", "obs_1", [])
    repos.passages.add_all("alice", "obs_1", [passage(0), passage(1)])
    assert repos.passages.list("alice", "obs_1") == []


def test_a_passage_for_another_observation_is_rejected_and_stores_nothing(repos):
    with pytest.raises(ValueError):
        repos.passages.add_all("alice", "obs_1", [passage(0), passage(1, obs="obs_2")])
    repos.passages.add_all("alice", "obs_1", [passage(0)])  # nothing was marked as split
    assert [p.index for p in repos.passages.list("alice", "obs_1")] == [0]


def test_the_split_marker_is_per_user(repos):
    repos.passages.add_all("alice", "obs_1", [])
    repos.passages.add_all("bob", "obs_1", [passage(0)])
    assert repos.passages.list("bob", "obs_1") != []
    assert repos.passages.list("alice", "obs_1") == []


# 3. Duplicate Report ids ---------------------------------------------------------------------

def test_a_duplicate_report_id_is_rejected_even_for_a_different_run(repos):
    repos.reports.add(report("rep1", run_id="r1"))
    with pytest.raises(ValueError):
        repos.reports.add(report("rep1", run_id="r2"))
    assert repos.reports.get("alice", "rep1").run_id == "r1"
    with pytest.raises(NotFoundError):
        repos.reports.get_for_run("alice", "r2")


def test_the_same_report_id_for_two_users_is_fine(repos):
    repos.reports.add(report("rep1", owner="alice", run_id="r1"))
    repos.reports.add(report("rep1", owner="bob", run_id="r1"))


# 4. SQLite durability ------------------------------------------------------------------------

def test_sqlite_data_survives_close_and_reopen_including_the_event_cursor(tmp_path):
    path = str(tmp_path / "durable.db")
    first = SqliteRepos(path)
    first.runs.add(run())
    obs = store_over(first).ingest_web(**KWARGS)
    first.passages.add_all("alice", "obs_empty", [])
    first.reports.add(report())
    seen = [first.events.append(event(step=i)) for i in range(3)]
    first.close()

    again = SqliteRepos(path)
    assert again.runs.get("alice", "r1") == run()
    contents = store_over(again).read_source("alice", obs.source_id)
    assert contents.observation == obs
    assert [p.text for p in contents.passages] == ["One.", "Two.", "Three."]
    assert again.reports.get("alice", "rep1") == report()

    # An empty split is still marked as split after reopening.
    again.passages.add_all("alice", "obs_empty", [passage(0, obs="obs_empty")])
    assert again.passages.list("alice", "obs_empty") == []

    # The cursor continues: nothing is lost or repeated.
    cursor = seen[0].seq
    assert [e.seq for e in again.events.read_after("alice", "r1", after_seq=cursor)] == [
        seen[1].seq, seen[2].seq,
    ]
    new = again.events.append(event(step=3))
    assert new.seq > seen[2].seq
    assert [e.seq for e in again.events.read_after("alice", "r1", after_seq=seen[2].seq)] == [new.seq]
    again.close()


def test_a_later_fetch_after_reopen_still_adds_an_observation(tmp_path):
    path = str(tmp_path / "durable.db")
    first = SqliteRepos(path)
    old = store_over(first).ingest_web(**KWARGS)
    first.close()
    again = SqliteRepos(path)
    new = store_over(again).ingest_web(**{**KWARGS, "fetched_at": T0 + timedelta(days=1)})
    assert new.source_id == old.source_id and new.id != old.id
    assert [o.id for o in again.observations.list_for_source("alice", old.source_id)] == [old.id, new.id]
