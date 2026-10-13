"""T13 (#14): the retention sweep, on SQLite files with in-memory Qdrant and a settable clock.

Each protection rule has its own test: Report Evidence, Historical items, fact-check verdicts,
Baseline seeds, established Findings and both Findings of a Diff entry. Then failure records,
the terminal-state and cutoff boundaries, shared Sources, extracted-fact provenance, the
Qdrant/SQLite failure cases, idempotency and restarts, owner isolation, malformed records and
a write racing the sweep."""

import json
from datetime import timedelta

import pytest

from insightforge_agent.domain.contracts import ExtractedFact
from insightforge_agent.domain.diff import DiffEntry, EstablishedFinding
from insightforge_agent.domain.models import RunEvent, RunState
from insightforge_agent.domain.retention import (
    LEGACY_UNRECORDED,
    FailureRecord,
    MalformedRetentionRoot,
    ProtectedRefs,
    expired,
    retention_cutoff,
)
from insightforge_agent.pipeline.citations import report_citations
from tests.stores.t13_world import DAYS, OLD, T0, YOUNG, World, finding, item, ref


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "app.db")
    yield w
    w.repos.close()


# --- Reports -------------------------------------------------------------------------------


def test_an_expired_complete_run_loses_uncited_material_and_keeps_its_report(world):
    world.web("r1", "obs_cited", paragraphs=1)
    world.web("r1", "obs_uncited", paragraphs=2)
    report = world.complete("r1", evidence=[item("ev1", ref("obs_cited"))])
    world.operational("r1")

    done = world.sweep()

    assert world.passages("obs_uncited") == [] and not world.has_observation("obs_uncited")
    assert not world.has_source("src_obs_uncited")
    assert world.passages("obs_cited") == [0] and world.has_source("src_obs_cited")
    assert world.repos.runs.get("alice", "r1").state == RunState.COMPLETE
    assert world.repos.reports.get("alice", report.id) == report
    assert world.repos.summaries.get_for_run("alice", "r1").memory_indexed
    assert world.sql("SELECT count(*) FROM subtasks")[0][0] == 0
    kept = {t for (t,) in world.sql("SELECT type FROM run_events WHERE run_id='r1'")}
    assert kept == {"stage", "run_failed"}
    assert done["passages"] == 2 and done["observations"] == 1


def test_a_cited_passage_keeps_its_observation_while_an_uncited_sibling_expires(world):
    world.web("r1", "obs_1", paragraphs=3)
    report = world.complete("r1", evidence=[item("ev1", ref("obs_1", 1))])

    world.sweep()

    assert world.passages("obs_1") == [1]
    assert world.has_observation("obs_1") and world.has_source("src_obs_1")
    (citation,) = report_citations(world.store, "alice", report)
    assert citation.passage_text == "obs_1 p1" and citation.source_title == "src_obs_1"


def test_supporting_and_contradicting_passages_of_an_evidence_item_are_kept(world):
    world.web("r1", "obs_1", paragraphs=5)
    world.complete("r1", evidence=[item("ev1", ref("obs_1", 0), supporting=[ref("obs_1", 1)],
                                        contradicting=[ref("obs_1", 2)])])
    world.sweep()
    assert world.passages("obs_1") == [0, 1, 2]


def test_a_historical_claim_keeps_the_older_reports_passages(world):
    world.web("r_old", "obs_old", paragraphs=3)
    world.complete("r_old", evidence=[item("ev_old", ref("obs_old", 0))])
    # A later Report's Historical item inherits passages 1 and 2 of the old Run.
    hist = world.historical_item("r_old", ref("obs_old", 1), supporting=[ref("obs_old", 2)])
    world.web("r_new", "obs_new", paragraphs=1)
    world.complete("r_new", at=OLD + timedelta(days=1), evidence=[item("ev_new",
                                                                       ref("obs_new"))],
                   historical=[hist])

    world.sweep()

    assert world.passages("obs_old") == [0, 1, 2]
    report = world.repos.reports.get("alice", "rep_r_new")
    assert [c.passage_text for c in report_citations(world.store, "alice", report)] == [
        "obs_new p0", "obs_old p1"]


def test_a_fact_check_verdict_keeps_its_original_and_matched_passages(world):
    world.web("r1", "obs_cited", paragraphs=2)
    world.web("r1", "obs_refetch", paragraphs=3)  # the Fact-Checker's new Observation
    world.complete("r1", evidence=[item("ev1", ref("obs_cited"))],
                   verdicts=[world.verdict(ref("obs_cited", 1), ref("obs_refetch", 2))])
    world.sweep()
    assert world.passages("obs_cited") == [0, 1]
    assert world.passages("obs_refetch") == [2]


def test_a_failed_runs_stored_report_still_protects_its_citations(world):
    """`settle_interrupted` can leave a Run FAILED with a Report it could not finalise."""
    world.web("r1", "obs_1", paragraphs=2)
    world.complete("r1", evidence=[item("ev1", ref("obs_1"))])
    world.execute("UPDATE runs SET state='FAILED' WHERE id='r1'")
    world.sweep()  # backfills the record: retention starts now
    world.now = T0 + timedelta(days=DAYS + 1)
    world.sweep()
    assert world.passages("obs_1") == [0]


# --- Watchlist provenance ------------------------------------------------------------------


def test_a_baseline_seed_keeps_its_evidence_and_observations(world):
    world.watchlist()
    world.web("r1", "obs_seed", paragraphs=3)
    world.web("r1", "obs_dated", paragraphs=1)
    world.complete("r1", item="w", evidence=[])  # the Report itself cites nothing here
    world.seed("r1", item("ev1", ref("obs_seed", 2)),
               observation_dates={"obs_seed": OLD, "obs_dated": OLD})

    world.sweep()

    assert world.passages("obs_seed") == [2]
    # Named only for its date: the Observation row stays, its unreferenced Passage goes.
    assert world.has_observation("obs_dated") and world.passages("obs_dated") == []
    assert world.repos.watchlists.baseline("alice", "w").seeds[0].evidence.primary == ref(
        "obs_seed", 2)


def test_an_established_finding_keeps_its_provenance(world):
    world.watchlist()
    world.web("r1", "obs_f", paragraphs=3)
    world.complete("r1", item="w")
    world.establish("k", EstablishedFinding(finding=finding("r1", ref("obs_f", 0),
                                                            supporting=[ref("obs_f", 1)])))
    world.sweep()
    assert world.passages("obs_f") == [0, 1]


def test_a_diff_entry_keeps_both_its_current_and_previous_findings(world):
    world.watchlist()
    world.web("r_prev", "obs_prev", paragraphs=2)
    world.web("r_cur", "obs_cur", paragraphs=2)
    world.complete("r_prev", item="w")
    world.complete("r_cur", item="w", at=OLD + timedelta(hours=1))
    # The established state has since moved on: only the Diff log still names these.
    world.diff_log("r_cur", [DiffEntry(outcome="changed", key="k",
                                       finding=finding("r_cur", ref("obs_cur", 1), value="$90"),
                                       previous=finding("r_prev", ref("obs_prev", 0)))])
    world.sweep()
    assert world.passages("obs_prev") == [0] and world.passages("obs_cur") == [1]


def test_the_baseline_normalisation_detail_protects_its_findings(world):
    world.watchlist()
    world.web("r1", "obs_n", paragraphs=2)
    world.complete("r1", item="w")
    entry = DiffEntry(outcome="added", key="k", finding=finding("r1", ref("obs_n", 1)))
    world.execute("INSERT INTO watchlist_baseline_state VALUES (?,?,?,?,?,?)", "alice", "w",
                  "r1", 1, T0.isoformat(),
                  f'{{"entries": [{entry.model_dump_json()}], "skipped": []}}')
    world.sweep()
    assert world.passages("obs_n") == [1]


# --- failure records and the terminal-state boundary ----------------------------------------


def test_an_expired_failed_run_keeps_only_its_failure_record_and_run(world):
    world.web("r1", "obs_1", paragraphs=3)
    world.operational("r1")
    world.failed("r1")

    world.sweep()

    assert not world.has_observation("obs_1") and not world.has_source("src_obs_1")
    run = world.repos.runs.get("alice", "r1")
    assert run.state == RunState.FAILED
    record = world.repos.runs.failure("alice", "r1")
    assert record == FailureRecord(
        owner_id="alice", run_id="r1", run_created_at=OLD, failed_at=OLD,
        retention_starts_at=OLD, stage="RESEARCHING", reason="every Sub-task failed",
        error_codes=["pipeline_failure"])
    assert {t for (t,) in world.sql("SELECT type FROM run_events")} == {"stage", "run_failed"}


def test_a_failed_run_younger_than_the_retention_period_is_untouched(world):
    world.web("r1", "obs_1", paragraphs=2)
    world.operational("r1")
    world.failed("r1", at=YOUNG)
    before = world.snapshot()
    world.sweep()
    assert world.snapshot() == before


@pytest.mark.parametrize("state", [s for s in RunState
                                   if s not in (RunState.COMPLETE, RunState.FAILED)])
def test_an_unfinished_run_is_never_cleaned_however_old(world, state):
    world.run("r1", state, created=OLD - timedelta(days=365))
    world.web("r1", "obs_1", paragraphs=2)
    world.operational("r1")
    before = world.snapshot()
    world.sweep()
    assert world.snapshot() == before


def test_a_complete_run_without_a_report_is_never_cleaned(world):
    world.run("r1", RunState.COMPLETE)
    world.web("r1", "obs_1")
    before = world.snapshot()
    world.sweep()
    assert world.snapshot() == before


def test_the_cutoff_is_inclusive_and_exact(world):
    cutoff = retention_cutoff(T0, DAYS)
    assert expired(cutoff, cutoff) and not expired(cutoff + timedelta(microseconds=1), cutoff)
    world.web("r_at", "obs_at")
    world.complete("r_at", at=cutoff)
    world.web("r_after", "obs_after")
    world.complete("r_after", at=cutoff + timedelta(microseconds=1))
    world.sweep()
    assert not world.has_observation("obs_at")
    assert world.has_observation("obs_after")


def test_retention_days_must_be_positive(world):
    with pytest.raises(ValueError):
        retention_cutoff(T0, 0)
    from insightforge_agent.config import Settings
    with pytest.raises(ValueError):
        Settings(llm_provider="fake", search_providers=["fake"], retention_days=0)
    assert Settings(llm_provider="fake", search_providers=["fake"]).retention_days == 30


def test_a_legacy_failed_run_is_backfilled_and_expires_from_backfill_time(world):
    # Failed long ago, before failure records existed: no record, created a year ago.
    world.run("r1", RunState.FAILED, created=T0 - timedelta(days=365))
    world.web("r1", "obs_1", paragraphs=2)
    world.repos.events.append(RunEvent(owner_id="alice", run_id="r1", type="run_failed",
                                       payload={"reason": "search providers all failed"},
                                       created_at=T0 - timedelta(days=365)))

    done = world.sweep()

    assert done["failures_backfilled"] == 1
    record = world.repos.runs.failure("alice", "r1")
    assert record.backfilled and record.failed_at is None and record.stage is None
    assert record.retention_starts_at == T0  # never Run.created_at
    assert record.error_codes == [LEGACY_UNRECORDED]
    assert record.reason == "search providers all failed"
    assert world.passages("obs_1") == [0, 1]

    world.now = T0 + timedelta(days=DAYS - 1)
    world.sweep()
    assert world.passages("obs_1") == [0, 1]
    world.now = T0 + timedelta(days=DAYS)
    world.sweep()
    assert not world.has_observation("obs_1")
    assert world.repos.runs.failure("alice", "r1") == record


def test_fail_is_atomic_with_its_record_and_keeps_the_first_record(world):
    world.run("r1", RunState.WRITING)
    failed = world.repos.runs.fail("alice", "r1", reason="x" * 5000,
                                   error_codes=["unexpected_error"], failed_at=YOUNG)
    assert failed.state == RunState.FAILED
    record = world.repos.runs.failure("alice", "r1")
    assert record.stage == "WRITING" and len(record.reason) == 1000
    world.repos.runs.fail("alice", "r1", reason="again", error_codes=["other"], failed_at=T0)
    assert world.repos.runs.failure("alice", "r1") == record
    with pytest.raises(ValueError):  # no codes: nothing written, the Run is unchanged
        world.run("r2", RunState.WRITING)
        world.repos.runs.fail("alice", "r2", reason="r", error_codes=[], failed_at=T0)
    assert world.repos.runs.get("alice", "r2").state == RunState.WRITING


# --- shared Sources ------------------------------------------------------------------------


def test_a_shared_source_survives_while_another_run_still_uses_it(world):
    url = "https://example.com/shared"
    world.web("r_old", "obs_old", source_id="src_shared", url=url)
    world.complete("r_old")
    world.run("r_live", RunState.RESEARCHING, created=T0)
    world.web("r_live", "obs_live", source_id="src_shared", url=url)
    world.run("r_linked", RunState.RESEARCHING, created=T0)
    world.repos.sources.add(world.repos.sources.get("alice", "src_shared").model_copy(
        update={"run_id": "r_linked"}))  # linked, Observation not stored yet

    world.sweep()

    assert not world.has_observation("obs_old")
    assert world.has_source("src_shared") and world.passages("obs_live") == [0, 1, 2]
    assert world.repos.sources.get("alice", "src_shared").run_id == "r_old"  # not authority


def test_a_source_first_fetched_by_an_expired_run_is_kept_for_a_live_runs_observation(world):
    world.web("r_old", "obs_old", source_id="src_s")
    world.complete("r_old")
    world.run("r_live", RunState.EXTRACTING, created=T0)
    world.web("r_live", "obs_live", source_id="src_s")
    world.sweep()
    assert world.has_source("src_s")
    assert world.store.read_run_source("alice", "r_live", "src_s").passages


# --- extracted facts -----------------------------------------------------------------------


def test_a_fact_whose_passage_expires_is_kept_with_provenance_unavailable(world):
    world.web("r1", "obs_1", paragraphs=2)
    world.complete("r1", evidence=[item("ev1", ref("obs_1", 0))])
    gone = world.fact("r1", ref("obs_1", 1), "src_obs_1", fid="fact_gone")
    kept = world.fact("r1", ref("obs_1", 0), "src_obs_1", fid="fact_kept")

    done = world.sweep()

    facts = {f.id: f for f in world.repos.entities.list_for_run("alice", "r1")}
    assert facts["fact_kept"] == kept
    detached = facts["fact_gone"]
    assert detached.provenance == "unavailable"
    assert detached.passage is None and detached.source_id is None
    assert detached.model_dump(exclude={"passage", "source_id", "provenance"}) == gone.model_dump(
        exclude={"passage", "source_id", "provenance"})
    assert done["facts_detached"] == 1


def test_fact_provenance_must_be_consistent():
    with pytest.raises(ValueError):
        ExtractedFact(id="f", source_id=None, passage=ref("o"), statement="s")
    with pytest.raises(ValueError):
        ExtractedFact(id="f", source_id="s", passage=ref("o"), statement="s",
                      provenance="unavailable")
    old = '{"id":"f","source_id":"s","passage":{"observation_id":"o","index":0},"statement":"s"}'
    assert ExtractedFact.model_validate_json(old).provenance == "available"


# --- uploaded documents and Qdrant ----------------------------------------------------------


def test_an_uncited_document_loses_its_chunks_and_points_a_cited_one_keeps_all(world):
    cited = world.pdf("r1", "cited.pdf")
    uncited = world.pdf("r1", "uncited.pdf")
    world.complete("r1", evidence=[item("ev1", ref(cited.id, 1))])

    world.sweep()

    # A document Claim is checked against every chunk of its Observation: all stay.
    assert world.passages(cited.id) == [0, 1, 2]
    assert not world.has_observation(uncited.id) and world.passages(uncited.id) == []
    assert {k[2] for k in world.point_keys()} == {cited.id}


def test_a_qdrant_failure_keeps_the_documents_passages_until_a_later_sweep(world):
    doc = world.pdf("r1")
    world.web("r1", "obs_web")
    world.complete("r1")

    def broken(*args, **kwargs):
        raise ConnectionError("Qdrant is down")
    real, world.index.delete = world.index.delete, broken

    done = world.sweep()

    assert done["chunk_removal_failed"] == 3
    assert world.passages(doc.id) == [0, 1, 2] and len(world.points()) == 3
    assert not world.has_observation("obs_web")  # web material is not held back

    world.index.delete = real
    world.sweep()
    assert not world.has_observation(doc.id) and world.points() == []


def test_a_crash_after_qdrant_and_before_sqlite_is_repaired_by_the_next_sweep(world):
    doc = world.pdf("r1")
    world.complete("r1")
    store, calls = world.repos.retention, []
    real_delete = store._delete_observation

    def crash(*args, **kwargs):  # after the Qdrant call, inside the SQLite transaction
        calls.append(1)
        raise RuntimeError("the process died mid-batch")
    store._delete_observation = crash
    done = world.sweep()
    assert done["owners_failed"] == 1 and calls
    assert world.points() == [] and world.passages(doc.id) == [0, 1, 2]  # SQLite rolled back

    store._delete_observation = real_delete
    world.restart()  # a new process, same files
    done = world.sweep()
    assert done["chunk_points_removed"] == 3  # deleting missing points is not an error
    assert not world.has_observation(doc.id)


def test_without_a_document_index_chunks_are_kept(world):
    doc = world.pdf("r1")
    world.complete("r1")
    world.sweeper.doc_index = None
    world.sweep()
    assert world.passages(doc.id) == [0, 1, 2]


def test_chunk_deletion_is_scoped_to_owner_and_run(world):
    mine = world.pdf("r1", owner="alice")
    theirs = world.pdf("r1", owner="bob")
    other_run = world.pdf("r2", name="b.pdf", owner="alice")
    refs = [ref(mine.id, i) for i in range(3)]
    world.index.delete("alice", "r2", refs)  # the right chunks under the wrong Run: no-op
    world.index.delete("bob", "r1", refs)  # the wrong owner: no-op
    assert len(world.points()) == 9
    world.index.delete("alice", "r1", refs)
    world.index.delete("alice", "r1", refs)  # again: idempotent
    assert {k[2] for k in world.point_keys()} == {theirs.id, other_run.id}


# --- idempotency, restarts and isolation ----------------------------------------------------


def test_a_repeated_sweep_and_a_restart_change_nothing_more(world):
    world.web("r1", "obs_1", paragraphs=3)
    world.pdf("r1")
    world.operational("r1")
    world.complete("r1", evidence=[item("ev1", ref("obs_1", 1))])
    world.failed("r2")
    world.web("r2", "obs_2")
    first = world.sweep()
    assert first["passages"] > 0
    after = world.snapshot()
    points = world.point_keys()

    second = world.sweep()
    assert world.snapshot() == after and world.point_keys() == points
    assert second["passages"] == second["observations"] == second["events"] == 0

    world.restart()
    world.sweep()
    assert world.snapshot() == after


def test_a_sweep_stopped_for_shutdown_is_finished_by_the_next_one(world):
    for n in range(3):
        world.web(f"r{n}", f"obs_{n}", paragraphs=150)
        world.complete(f"r{n}")
    world.sweeper.closing.set()
    assert world.sweep()["passages"] == 0
    world.restart()
    world.sweep()
    assert all(not world.has_observation(f"obs_{n}") for n in range(3))


def test_large_runs_are_cleaned_in_bounded_batches(world):
    world.web("r1", "obs_1", paragraphs=250)
    world.complete("r1")
    seen = []
    real_apply = world.repos.retention.apply

    def counting(owner_id, batch, cutoff, remove):
        seen.append(sum(len(o.indices) for o in batch.observations))
        return real_apply(owner_id, batch, cutoff, remove)
    world.repos.retention.apply = counting
    world.sweep()
    assert seen == [100, 100, 50] and not world.has_observation("obs_1")


def test_a_users_cleanup_never_touches_another_users_identical_ids(world):
    for owner in ("alice", "bob"):
        world.web("r1", "obs_1", paragraphs=2, owner=owner, source_id="src_1")
        world.fact("r1", ref("obs_1", 1), "src_1", owner=owner)
        world.pdf("r1", owner=owner)
    world.complete("r1", owner="alice")  # alice's Run expired, citing nothing
    world.complete("r1", owner="bob", at=YOUNG, evidence=[])  # bob's is young
    # alice's own Report would protect obs_1#1 for alice only; bob's for bob only.
    world.complete("r_cite", owner="bob", at=OLD, evidence=[item("ev", ref("obs_1", 1))])

    world.sweep()

    assert not world.has_observation("obs_1", owner="alice")
    assert world.passages("obs_1", owner="bob") == [0, 1]
    assert world.has_source("src_1", owner="bob")
    assert {p["owner_id"] for p in world.points()} == {"bob"} and len(world.points()) == 3
    (bobs,) = world.repos.entities.list_for_run("bob", "r1")
    assert bobs.provenance == "available"


# --- malformed records ---------------------------------------------------------------------


@pytest.mark.parametrize("table,setup", [
    ("reports", "UPDATE reports SET body='{\"evidence\": \"garbage\"}' WHERE id='rep_r1' AND owner_id='alice'"),
    ("reports", "UPDATE reports SET body='[1, 2]' WHERE id='rep_r1' AND owner_id='alice'"),
    ("reports", "UPDATE reports SET body='not json' WHERE id='rep_r1' AND owner_id='alice'"),
    ("diffs", "INSERT INTO watchlist_diffs (owner_id,item_id,run_id,status,detail,updated_at) "
              "VALUES ('alice','w','rX','succeeded','{\"entries\": [{\"outcome\": 1}]}','t')"),
    ("findings", "INSERT INTO watchlist_findings VALUES ('alice','w','k','{}')"),
    ("baselines", "INSERT INTO watchlist_baselines VALUES ('alice','w','{\"seeds\": 3}')"),
])
def test_a_malformed_retained_record_stops_cleanup_for_its_owner_only(world, table, setup):
    world.web("r1", "obs_1", paragraphs=2)
    world.complete("r1", evidence=[item("ev1", ref("obs_1"))])
    world.web("r1", "obs_1", paragraphs=2, owner="bob")
    world.complete("r1", owner="bob")
    world.execute(setup)
    before = world.sql("SELECT * FROM passages WHERE owner_id='alice'")

    done = world.sweep()

    assert done["owners_skipped_malformed"] == 1
    assert world.sql("SELECT * FROM passages WHERE owner_id='alice'") == before
    assert not world.has_observation("obs_1", owner="bob")


def test_an_unreadable_fact_row_keeps_its_passage(world):
    world.web("r1", "obs_1", paragraphs=1)
    world.complete("r1")
    world.execute("INSERT INTO entities VALUES ('alice','r1','f','{\"passage\": "
                  "{\"observation_id\": \"obs_1\", \"index\": 0}}')")
    world.sweep()
    assert world.passages("obs_1") == [0]


def test_the_collector_reads_references_from_fields_it_does_not_model():
    refs = ProtectedRefs()
    refs.add_report({"future": [{"observation_id": "o1", "index": 4}],
                     "dates": {"observation_dates": {"o2": "2030"}},
                     "deep": {"source_ids": ["s1"], "x": {"observation_id": "o3"}}}, "r")
    assert refs.passages == {("o1", 4)}
    assert refs.observations == {"o1", "o2", "o3"} and refs.sources == {"s1"}
    assert refs.whole == {"o3"}  # no index: the whole Observation; o2 only its row
    for bad in ({"observation_id": 7}, {"observation_id": "o", "index": "3"},
                {"observation_id": "o", "index": -1}, {"observation_id": "o", "index": True}):
        with pytest.raises(MalformedRetentionRoot):
            ProtectedRefs().add_report({"x": bad}, "r")
    with pytest.raises(MalformedRetentionRoot):
        ProtectedRefs().add_report({"draft": {"sections": "x"}}, "r")


# --- a write racing the sweep --------------------------------------------------------------


def test_evidence_protected_between_planning_and_deletion_is_kept(world):
    world.web("r1", "obs_1", paragraphs=3)
    world.complete("r1")
    store = world.repos.retention
    real_plan = store.plan

    def plan_then_cite(*args, **kwargs):
        batch = real_plan(*args, **kwargs)
        # Another thread stores a Report citing a planned Passage before the delete runs.
        world.web("r_late", "obs_late", paragraphs=1)
        world.complete("r_late", at=T0, evidence=[item("ev", ref("obs_1", 2))])
        store.plan = real_plan
        return batch
    store.plan = plan_then_cite

    world.sweep()

    assert world.passages("obs_1") == [2] and world.has_observation("obs_1")
    assert world.has_source("src_obs_1")


def test_a_live_run_linking_a_source_between_planning_and_deletion_keeps_it(world):
    world.web("r_old", "obs_old", source_id="src_s")
    world.complete("r_old")
    store = world.repos.retention
    real_plan = store.plan

    def plan_then_fetch(*args, **kwargs):
        batch = real_plan(*args, **kwargs)
        world.run("r_live", RunState.RESEARCHING, created=T0)
        world.repos.sources.add(world.repos.sources.get("alice", "src_s").model_copy(
            update={"run_id": "r_live"}))
        store.plan = real_plan
        return batch
    store.plan = plan_then_fetch

    world.sweep()

    assert not world.has_observation("obs_old") and world.has_source("src_s")
    assert [s.id for s in world.repos.sources.list_for_run("alice", "r_live")] == ["src_s"]


def test_a_document_chunk_protected_between_planning_and_deletion_keeps_its_point(world):
    doc = world.pdf("r1")
    world.complete("r1")
    store = world.repos.retention
    real_plan = store.plan

    def plan_then_cite(*args, **kwargs):
        batch = real_plan(*args, **kwargs)
        world.complete("r_late", at=T0, evidence=[item("ev", ref(doc.id, 1))])
        store.plan = real_plan
        return batch
    store.plan = plan_then_cite

    world.sweep()

    # Qdrant is called only after the check under the lock: nothing of it was removed.
    assert world.passages(doc.id) == [0, 1, 2] and len(world.points()) == 3


def test_an_uploaded_document_is_never_split_across_batches(world):
    doc = world.pdf("r1", chunks=150)
    world.web("r1", "obs_web", paragraphs=30)
    world.complete("r1")
    seen = []
    real_apply = world.repos.retention.apply

    def counting(owner_id, batch, cutoff, remove):
        seen.append([(o.uploaded, len(o.indices)) for o in batch.observations])
        return real_apply(owner_id, batch, cutoff, remove)
    world.repos.retention.apply = counting
    world.sweep()
    assert (True, 150) in seen[0]  # whole, even beyond the batch bound
    assert not world.has_observation(doc.id) and world.points() == []


def test_a_bare_observation_reference_protects_every_passage(world):
    world.web("r1", "obs_1", paragraphs=3)
    report = world.complete("r1")
    body = dict(report.body, future={"observation_id": "obs_1"})
    world.execute("UPDATE reports SET body=? WHERE id=?",
                  json.dumps(body), report.id)
    world.sweep()
    assert world.passages("obs_1") == [0, 1, 2]


def test_a_link_to_retained_material_stays_and_the_rest_go(world):
    world.web("r1", "obs_cited", paragraphs=2)
    world.web("r1", "obs_uncited", paragraphs=1)
    world.web("r1", "obs_dated", paragraphs=1)
    world.watchlist()
    world.complete("r1", item="w", evidence=[item("ev1", ref("obs_cited", 1))])
    world.seed("r1", item("ev1", ref("obs_cited", 1)), observation_dates={"obs_dated": OLD})

    world.sweep()

    listed = {s.id for s in world.repos.sources.list_for_run("alice", "r1")}
    assert listed == {"src_obs_cited", "src_obs_dated"}  # material the Run still holds
    after = world.snapshot()
    assert world.repos.retention.plan("alice", ["r1"], 100,
                                      world.repos.retention.protection("alice")).empty
    world.sweep()
    assert world.snapshot() == after


def test_chunk_deletion_has_a_bounded_timeout():
    from insightforge_agent.embeddings import HashingEmbedder
    from insightforge_agent.stores.doc_chunks import DELETE_TIMEOUT_SECONDS, DocChunkIndex

    class Recording:
        def __init__(self):
            self.calls = []

        def collection_exists(self, name):
            return True

        def get_collection(self, name):
            from types import SimpleNamespace
            return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(
                vectors=SimpleNamespace(size=64))))

        def delete(self, collection, **kwargs):
            self.calls.append(kwargs)

    client = Recording()
    DocChunkIndex(client, HashingEmbedder(64), "m", 64).delete("alice", "r1", [ref("o")])
    (call,) = client.calls
    assert call["timeout"] == DELETE_TIMEOUT_SECONDS and 0 < DELETE_TIMEOUT_SECONDS <= 30
    assert call["wait"] is True
