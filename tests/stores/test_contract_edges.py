"""Edge rules from review: behaviour that must match on the fakes and SQLite."""

from datetime import timedelta

import pytest

from insightforge_agent.domain import repositories as ports
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import sqlite_path
from tests.stores.builders import T0, event, observation, passage, report, run, source


def test_repositories_satisfy_their_interfaces(repos):
    pairs = [
        (repos.runs, ports.RunRepository), (repos.subtasks, ports.SubTaskRepository),
        (repos.sources, ports.SourceRepository), (repos.observations, ports.ObservationRepository),
        (repos.passages, ports.PassageRepository), (repos.reports, ports.ReportRepository),
        (repos.events, ports.RunEventRepository),
    ]
    for repo, port in pairs:
        assert isinstance(repo, port), f"{type(repo).__name__} does not satisfy {port.__name__}"


def test_a_duplicate_run_id_is_rejected(repos):
    repos.runs.add(run())
    with pytest.raises(ValueError):
        repos.runs.add(run())
    repos.runs.add(run(owner="bob"))  # same id, different User, is fine


def test_runs_and_reports_list_oldest_first(repos):
    from insightforge_agent.domain.models import Report

    repos.runs.add(run("late").model_copy(update={"created_at": T0 + timedelta(hours=1)}))
    repos.runs.add(run("early"))
    assert [r.id for r in repos.runs.list_for_owner("alice")] == ["early", "late"]
    repos.reports.add(Report(id="b", owner_id="alice", run_id="late",
                             created_at=T0 + timedelta(hours=1), body={}))
    repos.reports.add(Report(id="a", owner_id="alice", run_id="early", created_at=T0, body={}))
    assert [r.id for r in repos.reports.list_for_owner("alice")] == ["a", "b"]


def test_a_source_fetched_by_two_runs_is_listed_for_each(repos):
    repos.sources.add(source("src_1", run_id="r1"))
    repos.sources.add(source("src_2", run_id="r1"))
    again = repos.sources.add(source("src_1", run_id="r2"))
    assert again.run_id == "r1"  # the Source itself is never overwritten
    assert [s.id for s in repos.sources.list_for_run("alice", "r1")] == ["src_1", "src_2"]
    assert [s.id for s in repos.sources.list_for_run("alice", "r2")] == ["src_1"]
    assert repos.sources.list_for_run("bob", "r2") == []


def test_report_uniqueness_is_per_user_and_does_not_leak_across_users(repos):
    repos.reports.add(report("rep1", owner="alice", run_id="r1"))
    repos.reports.add(report("rep2", owner="bob", run_id="r1"))  # must not collide
    with pytest.raises(ValueError):
        repos.reports.add(report("rep3", owner="alice", run_id="r1"))


def test_duplicate_passage_index_is_rejected_and_stores_nothing(repos):
    with pytest.raises(ValueError):
        repos.passages.add_all("alice", [passage(0), passage(0, "again")])
    assert repos.passages.list("alice", "obs_1") == []


def test_events_can_be_built_without_a_dummy_seq():
    assert event().seq == 0


def test_an_observation_with_no_passages_is_still_never_re_split(repos, monkeypatch):
    from insightforge_agent.stores import source_store as module

    store = SourceStore(repos.sources, repos.observations, repos.passages)
    kwargs = dict(owner_id="alice", run_id="r1", url="https://example.com/empty",
                  title="Empty", body="   ", fetched_at=T0)
    first = store.ingest_web(**kwargs)
    assert repos.passages.list("alice", first.id) == []
    monkeypatch.setattr(module, "split_paragraphs", lambda body: ["now it splits"])
    again = store.ingest_web(**kwargs)
    assert again.id == first.id
    assert repos.passages.list("alice", first.id) == []


def test_second_run_fetching_the_same_url_sees_it_in_its_own_run(repos):
    store = SourceStore(repos.sources, repos.observations, repos.passages)
    for run_id, offset in (("r1", 0), ("r2", 1)):
        store.ingest_web(owner_id="alice", run_id=run_id, url="https://example.com/x",
                         title="X", body="Same page.", fetched_at=T0 + timedelta(days=offset))
    assert [s.title for s in repos.sources.list_for_run("alice", "r2")] == ["X"]
    assert len(repos.observations.list_for_source("alice", repos.sources.list_for_run("alice", "r1")[0].id)) == 2


def test_observation_still_resolves_after_other_users_activity(repos):
    repos.observations.add(observation())
    repos.observations.add(observation(owner="bob"))
    assert repos.observations.get("bob", "obs_1").owner_id == "bob"


@pytest.mark.parametrize("url,expected", [
    ("sqlite:///insightforge.db", "insightforge.db"),
    ("sqlite:///data/app.db", "data/app.db"),
    ("sqlite:////var/lib/app.db", "/var/lib/app.db"),
    ("sqlite://", ":memory:"),
    ("sqlite:///:memory:", ":memory:"),
])
def test_sqlite_path_from_database_url(url, expected):
    assert sqlite_path(url) == expected


def test_sqlite_path_rejects_other_schemes():
    with pytest.raises(ValueError):
        sqlite_path("postgresql://x/y")
