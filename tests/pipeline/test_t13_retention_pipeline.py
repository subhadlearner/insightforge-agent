"""T13 (#14) over the real pipeline (scripted models, fixture pages, hashing embedder, in-memory
Qdrant, SQLite): every failure path leaves a failure record in the same write as FAILED, and a
retention sweep after real Runs keeps citations, memory eligibility and the inherited
fact-checks of Historical Claims working."""

from datetime import timedelta

import pytest

from insightforge_agent.domain import retention
from insightforge_agent.domain.contracts import Brief, ExtractedFact, PassageRef
from insightforge_agent.domain.models import Clarification, Report, Run, RunState
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.pipeline.clarification import ClarificationService
from insightforge_agent.pipeline.graph import (
    await_clarification,
    carry_run,
    check_brief,
    resume_run,
    run_brief,
    settle_interrupted,
    start_run,
)
from insightforge_agent.pipeline.memory import eligible_report_ids
from insightforge_agent.pipeline.recovery import startup_sweep
from insightforge_agent.pipeline.research import ResearchService
from insightforge_agent.scheduler.retention import RetentionSweeper
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.pipeline.test_t8_memory import (  # noqa: F401 - mem_deps is a fixture
    MEMORY_ONLY,
    T0,
    BrokenIndex,
    claims_of,
    mem_deps,
    memory_run,
    prior_run,
    report_of,
)
from tests.scripted import ScriptedChat


@pytest.fixture
def sql_deps(mem_deps):  # noqa: F811
    made = []

    def make(**overrides):
        repos = SqliteRepos(":memory:")
        made.append(repos)
        return mem_deps(repos=repos, store=SourceStore(repos.sources, repos.observations,
                                                       repos.passages), **overrides)
    yield make
    for repos in made:
        repos.close()


def record_of(deps, run):
    return deps.repos.runs.failure(run.owner_id, run.id)


def plain_run(deps, run_id="run_x", state=RunState.PLANNING, **kw) -> Run:
    run = Run(id=run_id, owner_id="alice", brief="EV market", state=state,
              created_at=deps.now(), **kw)
    deps.repos.runs.add(run)
    return run


# --- failure records on every path to FAILED ------------------------------------------------


def test_a_stage_failure_records_its_stage_reason_and_code(sql_deps):
    deps = sql_deps(run_token_cap=1)
    run = run_brief(deps, "alice", Brief(text="EV market"))
    assert run.state == RunState.FAILED
    record = record_of(deps, run)
    assert record.error_codes == [retention.PIPELINE_FAILURE]
    assert "token cap of 1" in record.reason
    assert record.stage == "EXTRACTING" and record.failed_at == deps.now()
    assert record.retention_starts_at == record.failed_at and not record.backfilled


def test_an_unexpected_error_is_recorded_as_such(sql_deps):
    def explode(messages):
        raise RuntimeError("provider exploded")
    deps = sql_deps()
    run = start_run(deps, check_brief(deps, "alice", Brief(text="EV market")))
    deps.planner_model = ScriptedChat(reply=explode)
    failed = carry_run(deps, run)
    record = record_of(deps, failed)
    assert record.error_codes == [retention.UNEXPECTED_ERROR]
    assert record.stage == "PLANNING" and "RuntimeError: provider exploded" in record.reason


def test_the_startup_sweep_records_an_interrupted_run(sql_deps):
    deps = sql_deps()
    run = plain_run(deps, state=RunState.SYNTHESIZING)
    startup_sweep(deps, ClarificationService(deps, submit=lambda work: None))
    record = record_of(deps, run)
    assert record.error_codes == [retention.PROCESS_INTERRUPTED]
    assert record.stage == "SYNTHESIZING"


def test_shutdown_cancellation_records_a_run_that_never_started(sql_deps):
    deps = sql_deps()
    started = start_run(deps, check_brief(deps, "alice", Brief(text="EV market")))
    ResearchService(deps, executor=None)._not_started(started)
    record = record_of(deps, started.run)
    assert record.error_codes == [retention.NOT_STARTED] and record.stage == "PLANNING"


def test_a_report_that_cannot_be_finalised_records_both_codes(sql_deps):
    deps = sql_deps(report_index=BrokenIndex())
    run = plain_run(deps, state=RunState.FACT_CHECKING)
    deps.repos.reports.add(Report(id="rep_x", owner_id="alice", run_id=run.id,
                                  created_at=deps.now(), body={"draft": {"title": "t",
                                                                         "sections": []}}))
    settled = settle_interrupted(deps, "alice", run.id, "the process stopped",
                                 retention.PROCESS_INTERRUPTED)
    assert settled.state == RunState.FAILED
    record = record_of(deps, settled)
    assert record.error_codes == [retention.PROCESS_INTERRUPTED,
                                  retention.REPORT_NOT_FINALISED]
    assert "could not be finalised" in record.reason


def test_a_resume_without_its_checkpoint_and_a_clarification_conflict_are_recorded(sql_deps):
    deps = sql_deps()
    asked = deps.now()
    resolved = Clarification(question="q?", asked_at=asked, deadline=asked, resolution="timed_out",
                             resolved_at=asked)
    resuming = plain_run(deps, "run_r", RunState.RESUMING, clarification=resolved)
    resume_run(deps, "alice", resuming.id)
    assert record_of(deps, resuming).error_codes == [retention.CHECKPOINT_MISSING]

    busy = plain_run(deps, "run_c", RunState.RESEARCHING)
    await_clarification(deps, "alice", busy.id, "q?")
    record = record_of(deps, busy)
    assert record.error_codes == [retention.CLARIFICATION_CONFLICT]
    assert record.stage == "RESEARCHING"


def test_a_completed_run_has_no_failure_record(sql_deps):
    deps = sql_deps()
    run = prior_run(deps)
    with pytest.raises(Exception):
        record_of(deps, run)


# --- the sweep after real Runs ---------------------------------------------------------------


def sweep(deps, days=30):
    return RetentionSweeper(deps.repos, deps.doc_index, deps.now, retention_days=days).sweep()


def test_after_a_sweep_citations_memory_and_inherited_fact_checks_still_work(sql_deps):
    deps = sql_deps()
    prior = prior_run(deps)  # day 0: every fixture paragraph ends up cited
    report = report_of(deps, prior)
    # A search result the Run fetched but never cited, and a fact extracted from it.
    unused = deps.store.ingest_web(owner_id="alice", run_id=prior.id, url="https://x.test/u",
                                   title="Unused", body="Acme sold 5 cars.\n\nOther.",
                                   fetched_at=T0)
    deps.repos.entities.upsert_facts("alice", prior.id, [ExtractedFact(
        id="fact_unused", source_id=unused.source_id, statement="Acme sold 5 cars.",
        passage=PassageRef(observation_id=unused.id, index=0), entity="Acme")])
    citations = report_citations(deps.store, "alice", report)
    facts = deps.repos.entities.list_for_run("alice", prior.id)

    deps.now.t = T0 + timedelta(days=31)
    done = sweep(deps)

    assert done["passages"] == 2 and done["observations"] == done["sources"] == 1
    assert done["facts_detached"] == 1
    assert report_citations(deps.store, "alice", report) == citations
    assert eligible_report_ids(deps, "alice", None) == [report.id]
    after = {f.id: f for f in deps.repos.entities.list_for_run("alice", prior.id)}
    assert set(after) == {f.id for f in facts}  # no fact is lost
    for fact in after.values():
        if fact.provenance == "available":
            deps.store.citation("alice", fact.passage)  # never a dangling reference
        else:
            assert fact.passage is None and fact.source_id is None
    assert after["fact_unused"].provenance == "unavailable"

    # A later memory Run inherits the old Claims and fact-checks them against their
    # original Sources, which the sweep kept.
    later = memory_run(deps, MEMORY_ONLY, web=False, day=35)
    assert later.state == RunState.COMPLETE
    later_report = report_of(deps, later)
    assert claims_of(later_report) and all(c.historical for c in claims_of(later_report))
    verdicts = later_report.body["verified"]["verdicts"]
    assert verdicts and all(v["verdict"] == "verified" for v in verdicts)
    report_citations(deps.store, "alice", later_report)

    # Swept again much later: the later Report now protects the same Passages too.
    deps.now.t = T0 + timedelta(days=120)
    sweep(deps)
    assert report_citations(deps.store, "alice", report) == citations
    report_citations(deps.store, "alice", later_report)


def test_failed_run_material_never_reaches_memory_and_expires(sql_deps):
    deps = sql_deps(run_token_cap=1)
    failed = run_brief(deps, "alice", Brief(text="EV market"))
    assert failed.state == RunState.FAILED
    assert eligible_report_ids(deps, "alice", None) == []
    observations = deps.repos._db.execute(
        "SELECT count(*) FROM observations WHERE run_id=?", (failed.id,)).fetchone()[0]
    assert observations > 0

    deps.now.t = deps.now.t + timedelta(days=30)
    sweep(deps)

    assert deps.repos._db.execute("SELECT count(*) FROM observations WHERE run_id=?",
                                  (failed.id,)).fetchone()[0] == 0
    assert deps.repos.runs.get("alice", failed.id).state == RunState.FAILED
    assert "token cap" in deps.repos.runs.failure("alice", failed.id).reason
