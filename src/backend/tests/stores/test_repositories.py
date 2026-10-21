"""Repository contract: identical behaviour on the in-memory fakes and SQLite."""

from datetime import timedelta

import pytest

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import RunState, SubTaskStatus
from tests.stores.builders import (
    T0,
    event,
    observation,
    passage,
    report,
    run,
    source,
    subtask,
)


class TestRuns:
    def test_add_and_get(self, repos):
        repos.runs.add(run())
        assert repos.runs.get("alice", "r1") == run()

    def test_other_user_cannot_get_it(self, repos):
        repos.runs.add(run())
        with pytest.raises(NotFoundError):
            repos.runs.get("bob", "r1")

    def test_missing_run_is_not_found(self, repos):
        with pytest.raises(NotFoundError):
            repos.runs.get("alice", "nope")

    def test_list_returns_only_own_runs(self, repos):
        repos.runs.add(run("r1", "alice"))
        repos.runs.add(run("r2", "bob"))
        assert [r.id for r in repos.runs.list_for_owner("alice")] == ["r1"]

    def test_set_state(self, repos):
        repos.runs.add(run())
        updated = repos.runs.set_state("alice", "r1", RunState.RESEARCHING)
        assert updated.state is RunState.RESEARCHING
        assert repos.runs.get("alice", "r1").state is RunState.RESEARCHING

    def test_other_user_cannot_change_state(self, repos):
        repos.runs.add(run())
        with pytest.raises(NotFoundError):
            repos.runs.set_state("bob", "r1", RunState.FAILED)
        assert repos.runs.get("alice", "r1").state is RunState.PLANNING


class TestSubTasks:
    def test_plan_round_trips_in_order(self, repos):
        recs = [subtask("s1"), subtask("s2"), subtask("s3")]
        repos.subtasks.save_plan("alice", "r1", recs)
        assert repos.subtasks.list("alice", "r1") == recs

    def test_set_status(self, repos):
        repos.subtasks.save_plan("alice", "r1", [subtask("s1"), subtask("s2")])
        repos.subtasks.set_status("alice", "r1", "s2", SubTaskStatus.FAILED)
        by_id = {r.subtask.id: r.status for r in repos.subtasks.list("alice", "r1")}
        assert by_id == {"s1": SubTaskStatus.PENDING, "s2": SubTaskStatus.FAILED}

    def test_other_user_sees_nothing_and_cannot_change(self, repos):
        repos.subtasks.save_plan("alice", "r1", [subtask("s1")])
        assert repos.subtasks.list("bob", "r1") == []
        with pytest.raises(NotFoundError):
            repos.subtasks.set_status("bob", "r1", "s1", SubTaskStatus.FAILED)

    def test_unknown_subtask_is_not_found(self, repos):
        with pytest.raises(NotFoundError):
            repos.subtasks.set_status("alice", "r1", "s9", SubTaskStatus.FAILED)


class TestSources:
    def test_add_and_get(self, repos):
        repos.sources.add(source())
        assert repos.sources.get("alice", "src_1") == source()

    def test_add_is_idempotent_and_never_overwrites(self, repos):
        repos.sources.add(source())
        again = repos.sources.add(source().model_copy(update={"title": "Changed"}))
        assert again.title == "Example"
        assert repos.sources.get("alice", "src_1").title == "Example"

    def test_same_id_for_two_users_stays_separate(self, repos):
        repos.sources.add(source(owner="alice"))
        repos.sources.add(source(owner="bob").model_copy(update={"title": "Bobs"}))
        assert repos.sources.get("alice", "src_1").title == "Example"
        assert repos.sources.get("bob", "src_1").title == "Bobs"

    def test_other_user_cannot_get_it(self, repos):
        repos.sources.add(source())
        with pytest.raises(NotFoundError):
            repos.sources.get("bob", "src_1")

    def test_list_for_run_is_scoped(self, repos):
        repos.sources.add(source("src_1", run_id="r1"))
        repos.sources.add(source("src_2", run_id="r2"))
        assert [s.id for s in repos.sources.list_for_run("alice", "r1")] == ["src_1"]
        assert repos.sources.list_for_run("bob", "r1") == []


class TestObservationsAndPassages:
    def test_observation_add_is_idempotent(self, repos):
        repos.observations.add(observation())
        repos.observations.add(observation())
        assert repos.observations.get("alice", "obs_1") == observation()
        assert len(repos.observations.list_for_source("alice", "src_1")) == 1

    def test_observations_for_a_source_are_oldest_first(self, repos):
        repos.observations.add(observation("obs_late", at=T0 + timedelta(days=1)))
        repos.observations.add(observation("obs_early", at=T0))
        ids = [o.id for o in repos.observations.list_for_source("alice", "src_1")]
        assert ids == ["obs_early", "obs_late"]

    def test_passages_round_trip_in_index_order(self, repos):
        ps = [passage(2), passage(0), passage(1, page=3, section_heading="Intro")]
        repos.passages.add_all("alice", "obs_1", ps)
        listed = repos.passages.list("alice", "obs_1")
        assert [p.index for p in listed] == [0, 1, 2]
        assert listed[1].page == 3 and listed[1].section_heading == "Intro"

    def test_get_passage_by_identity(self, repos):
        repos.passages.add_all("alice", "obs_1", [passage(0), passage(1)])
        assert repos.passages.get("alice", "obs_1", 1).text == "paragraph 1"
        with pytest.raises(NotFoundError):
            repos.passages.get("alice", "obs_1", 7)

    def test_an_observation_is_never_re_split(self, repos):
        repos.passages.add_all("alice", "obs_1", [passage(0, "original"), passage(1, "original 1")])
        repos.passages.add_all("alice", "obs_1", [passage(0, "different split")])
        listed = repos.passages.list("alice", "obs_1")
        assert [p.text for p in listed] == ["original", "original 1"]

    def test_other_user_cannot_read_passages(self, repos):
        repos.observations.add(observation())
        repos.passages.add_all("alice", "obs_1", [passage(0)])
        assert repos.passages.list("bob", "obs_1") == []
        with pytest.raises(NotFoundError):
            repos.passages.get("bob", "obs_1", 0)
        with pytest.raises(NotFoundError):
            repos.observations.get("bob", "obs_1")


class TestReports:
    def test_add_get_and_get_for_run(self, repos):
        repos.reports.add(report())
        assert repos.reports.get("alice", "rep1") == report()
        assert repos.reports.get_for_run("alice", "r1").id == "rep1"

    def test_one_report_per_run(self, repos):
        repos.reports.add(report("rep1"))
        with pytest.raises(ValueError):
            repos.reports.add(report("rep2"))

    def test_other_user_cannot_read(self, repos):
        repos.reports.add(report())
        with pytest.raises(NotFoundError):
            repos.reports.get("bob", "rep1")
        with pytest.raises(NotFoundError):
            repos.reports.get_for_run("bob", "r1")
        assert repos.reports.list_for_owner("bob") == []

    def test_list_for_owner(self, repos):
        repos.reports.add(report("rep1", run_id="r1"))
        repos.reports.add(report("rep2", run_id="r2"))
        assert {r.id for r in repos.reports.list_for_owner("alice")} == {"rep1", "rep2"}


class TestRunEvents:
    def test_append_assigns_increasing_seq(self, repos):
        a = repos.events.append(event(step=1))
        b = repos.events.append(event(step=2))
        assert 0 < a.seq < b.seq

    def test_read_after_a_cursor_loses_nothing(self, repos):
        first = [repos.events.append(event(step=i)) for i in range(3)]
        later = repos.events.append(event(step=3))
        got = repos.events.read_after("alice", "r1", after_seq=first[0].seq)
        assert [e.seq for e in got] == [first[1].seq, first[2].seq, later.seq]
        assert [e.payload["step"] for e in got] == [1, 2, 3]

    def test_reading_from_the_last_seen_returns_only_new_events(self, repos):
        e = repos.events.append(event())
        assert repos.events.read_after("alice", "r1", after_seq=e.seq) == []
        new = repos.events.append(event())
        assert repos.events.read_after("alice", "r1", after_seq=e.seq) == [new]

    def test_cursor_zero_returns_everything_and_limit_pages(self, repos):
        ids = [repos.events.append(event(step=i)).seq for i in range(5)]
        assert [e.seq for e in repos.events.read_after("alice", "r1")] == ids
        page = repos.events.read_after("alice", "r1", limit=2)
        assert [e.seq for e in page] == ids[:2]
        rest = repos.events.read_after("alice", "r1", after_seq=page[-1].seq)
        assert [e.seq for e in rest] == ids[2:]

    def test_events_are_scoped_to_run_and_owner(self, repos):
        repos.events.append(event(run_id="r1"))
        repos.events.append(event(run_id="r2"))
        assert [e.run_id for e in repos.events.read_after("alice", "r1")] == ["r1"]
        assert repos.events.read_after("bob", "r1") == []

    def test_payload_round_trips(self, repos):
        repos.events.append(event(type="progress", stage="PLANNING", done=2, notes=["a", "b"]))
        (got,) = repos.events.read_after("alice", "r1")
        assert got.type == "progress"
        assert got.payload == {"stage": "PLANNING", "done": 2, "notes": ["a", "b"]}

    def test_there_is_no_way_to_change_or_remove_events(self, repos):
        assert not any(hasattr(repos.events, n) for n in ("update", "delete", "remove", "clear"))


class TestEntities:
    @staticmethod
    def fact(fid="f1", **kw):
        from insightforge_agent.domain.contracts import ExtractedFact, PassageRef

        return ExtractedFact(id=fid, source_id="s1", passage=PassageRef(observation_id="o1", index=0),
                             statement="BYD sold 4.27 million", entity="BYD", **kw)

    def test_facts_round_trip_in_order_per_run(self, repos):
        repos.entities.upsert_facts("alice", "r1", [self.fact("f1"), self.fact("f2")])
        repos.entities.upsert_facts("alice", "r2", [self.fact("f3")])
        assert [f.id for f in repos.entities.list_for_run("alice", "r1")] == ["f1", "f2"]

    def test_storing_a_fact_again_replaces_it_in_place(self, repos):
        repos.entities.upsert_facts("alice", "r1", [self.fact("f1"), self.fact("f2")])
        repos.entities.upsert_facts("alice", "r1", [self.fact("f1", conflict=True)])
        facts = repos.entities.list_for_run("alice", "r1")
        assert [(f.id, f.conflict) for f in facts] == [("f1", True), ("f2", False)]

    def test_other_user_sees_nothing(self, repos):
        repos.entities.upsert_facts("alice", "r1", [self.fact("f1")])
        assert repos.entities.list_for_run("bob", "r1") == []
