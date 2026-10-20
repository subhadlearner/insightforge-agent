"""T18 Stage C: a Report whose Evidence carries a publication date is stored in SQLite.

Both Stage C Runs (run_d2bd97047785, run_600c859dd6fe) failed at the last step with
`TypeError: Object of type date is not JSON serializable`: `EvidenceItem.published_at` is a
`date`, and the Report body held the Evidence bundle in Python mode. The in-memory repositories
never serialise the body, so only a SQLite-backed Run shows it."""

from dataclasses import replace
from datetime import UTC, date, datetime

from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.domain.contracts import (
    Brief,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    PassageRef,
)
from insightforge_agent.domain.models import Report, RunState
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.pipeline import fakes

BRIEF = "Compare BYD and Tesla in electric vehicles: sales, pricing, battery technology."
PUBLISHED = datetime(2025, 6, 2, 9, 30, tzinfo=UTC)


class DatedSearch(fakes.FixtureSearch):
    """The fixture pages, each with page metadata giving a publication date."""

    def fetch(self, url: str):
        return replace(super().fetch(url), published_at=PUBLISHED)


def test_a_report_with_a_dated_evidence_item_round_trips_through_sqlite(tmp_path):
    item = EvidenceItem(
        id="ev_1", subtask_id="s1", aspect_id="a1", statement="BYD sold 4.27 million vehicles in 2024.",
        confidence="HIGH", supporting=[], primary=PassageRef(observation_id="obs_1", index=0),
        published_at=date(2025, 6, 2))
    bundle = EvidenceBundle(sections=[EvidenceSection(
        aspect_id="a1", heading="Sales", confidence="HIGH", items=[item])])
    repos = SqliteRepos(str(tmp_path / "app.db"))
    try:
        repos.reports.add(Report(id="rep_1", owner_id="alice", run_id="run_1",
                                 created_at=datetime(2026, 10, 21, tzinfo=UTC),
                                 body={"evidence": bundle.model_dump(mode="json")}))
        stored = repos.reports.get_for_run("alice", "run_1")
    finally:
        repos.close()
    restored = EvidenceBundle.model_validate(stored.body["evidence"])
    assert restored.sections[0].items[0].published_at == date(2025, 6, 2)
    assert restored == bundle


def test_a_run_whose_evidence_is_dated_completes_and_stores_its_report(tmp_path):
    repos = SqliteRepos(str(tmp_path / "app.db"))
    with SqliteSaver.from_conn_string(str(tmp_path / "ck.db")) as saver:
        search = DatedSearch()
        deps = Deps(repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
                    planner_model=fakes.planner(), writer_model=fakes.writer(),
                    light_model=fakes.light(), researcher_model=fakes.researcher(),
                    search=search, fetcher=search, checkpointer=saver)
        run = run_brief(deps, "alice", Brief(text=BRIEF))
    repos.close()

    reopened = SqliteRepos(str(tmp_path / "app.db"))  # a new connection: what was really stored
    try:
        events = reopened.events.read_after("alice", run.id, limit=10_000)
        assert run.state == RunState.COMPLETE, [e.payload for e in events if e.type == "run_failed"]
        assert reopened.runs.get("alice", run.id).state == RunState.COMPLETE
        report = reopened.reports.get_for_run("alice", run.id)
        summary = reopened.summaries.get_for_run("alice", run.id)
    finally:
        reopened.close()

    bundle = EvidenceBundle.model_validate(report.body["evidence"])
    dates = {i.published_at for s in bundle.sections for i in s.items}
    assert dates == {PUBLISHED.date()}
    claims = [c for s in report.body["draft"]["sections"] for c in s["claims"]]
    assert claims
    assert all(c["evidence_id"] in {i.id for s in bundle.sections for i in s.items} for c in claims)
    assert summary.run_id == run.id
    assert [e.payload["report_id"] for e in events if e.type == "run_summary_stored"] == [report.id]
