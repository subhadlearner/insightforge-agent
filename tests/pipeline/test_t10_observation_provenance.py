"""T10: a Run reads only its own Observations of a web Source.

A web Source's identity is its URL, so two Runs of one User that fetch the same page share the
Source and each add an Observation. Extraction and synthesis must read the Observation *their*
Run fetched, never the User's latest one (which, with concurrent Runs, may be another Run's),
and two Runs' fetches must never collapse into one Observation, even with identical content
and timestamps. Report citations keep resolving to the exact original Observation and Passage."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.agents.web import Page
from insightforge_agent.domain.contracts import Brief, EvidenceBundle
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.ids import derive_observation_id, derive_source_id
from insightforge_agent.domain.models import Run, RunState
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.pipeline import fakes
from tests.scripted import ScriptedChat

T0 = datetime(2030, 3, 1, 9, tzinfo=UTC)
URL = "https://ev.example/byd-2025"


class Tagged(fakes.FixtureSearch):
    """The fixture pages, with every paragraph marked as fetched by one Run."""

    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag

    def fetch(self, url):
        page = super().fetch(url)
        paragraphs = [p for p in page.body.split("\n\n") if p.strip()]
        return Page(url=page.url, title=page.title,
                    body="\n\n".join(f"{self.tag} {p}" for p in paragraphs))


def light_after(barrier: threading.Barrier | None) -> ScriptedChat:
    """The scripted light model; the first Extraction call waits until the other Run has
    finished researching too, so both Runs have fetched every page before either extracts."""
    base, waited = fakes.light(), []

    def reply(messages):
        if barrier is not None and not waited and "You extract facts" in str(messages[0].content):
            waited.append(True)
            barrier.wait(60)
        return base.reply(messages)
    return ScriptedChat(reply=reply)


@pytest.fixture
def shared():
    """One User's repositories and checkpointer, shared by several Runs (as in the app)."""
    with SqliteSaver.from_conn_string(":memory:") as saver:
        repos = SqliteRepos()
        store = SourceStore(repos.sources, repos.observations, repos.passages)

        def deps_for(search: fakes.FixtureSearch, barrier=None, now=lambda: T0) -> Deps:
            return Deps(repos=repos, store=store, planner_model=fakes.planner(),
                        writer_model=fakes.writer(), light_model=light_after(barrier),
                        researcher_model=fakes.researcher(), search=search, fetcher=search,
                        checkpointer=saver, now=now)
        yield repos, store, deps_for
        repos.close()


def assert_reads_only_its_own(deps: Deps, owner: str, run: Run, tag: str) -> None:
    """Every Passage the Run extracted from, built Evidence on and cites is from an Observation
    that Run fetched, and holds that Run's content."""
    assert run.state == RunState.COMPLETE, run
    repos = deps.repos

    def own(observation_id: str) -> None:
        assert repos.observations.get(owner, observation_id).run_id == run.id

    facts = repos.entities.list_for_run(owner, run.id)
    assert facts
    for f in facts:
        own(f.passage.observation_id)
        assert f.statement.startswith(tag)
    report = repos.reports.get_for_run(owner, run.id)
    bundle = EvidenceBundle.model_validate(report.body["evidence"])
    items = [i for s in bundle.sections for i in s.items]
    assert items
    for item in items:
        assert item.statement.startswith(tag)
        for ref in [item.primary, *item.supporting]:
            own(ref.observation_id)
    for citation in report_citations(deps.store, owner, report):
        own(citation.passage.observation_id)
        assert citation.passage_text.startswith(tag)


# --- identity -------------------------------------------------------------------------------

def test_two_runs_fetching_identical_content_at_the_same_instant_get_their_own_observations(
        shared):
    repos, store, _ = shared
    kw = dict(owner_id="alice", url=URL, title="BYD", body="Same body.", fetched_at=T0)
    a = store.ingest_web(run_id="run_a", **kw)
    b = store.ingest_web(run_id="run_b", **kw)
    assert a.id != b.id
    assert (a.run_id, b.run_id) == ("run_a", "run_b")
    assert a.source_id == b.source_id == derive_source_id("web", URL)  # one Source
    assert store.ingest_web(run_id="run_a", **kw).id == a.id  # still idempotent within a Run
    assert derive_observation_id("alice", "run_a", "src", T0, "x") != \
        derive_observation_id("alice", "run_b", "src", T0, "x")


def test_a_run_reads_its_own_latest_observation_never_another_runs(shared):
    repos, store, _ = shared
    mine = store.ingest_web(owner_id="alice", run_id="run_a", url=URL, title="BYD",
                            body="Alpha.", fetched_at=T0)
    store.ingest_web(owner_id="alice", run_id="run_b", url=URL, title="BYD", body="Beta.",
                     fetched_at=T0 + timedelta(hours=1))  # later, by another Run
    contents = store.read_run_source("alice", "run_a", mine.source_id)
    assert contents.observation.id == mine.id
    assert [p.text for p in contents.passages] == ["Alpha."]
    assert store.read_run_source("alice", "run_c", mine.source_id).observation is None
    with pytest.raises(NotFoundError):
        store.read_run_source("bob", "run_a", mine.source_id)


def test_extraction_reads_the_runs_own_fetch_when_another_run_fetched_the_url_since(shared):
    repos, store, deps_for = shared
    deps = deps_for(fakes.FixtureSearch())
    store.ingest_web(owner_id="alice", run_id="run_a", url=URL, title="BYD",
                     body="Alpha sold 10 cars.\n\nAlpha priced at 5.", fetched_at=T0)
    store.ingest_web(owner_id="alice", run_id="run_b", url=URL, title="BYD",
                     body="Beta sold 99 cars.", fetched_at=T0 + timedelta(minutes=5))
    result = extract(deps, "alice", "run_a", [derive_source_id("web", URL)])
    assert [f.statement for f in result.facts] == ["Alpha sold 10 cars.", "Alpha priced at 5."]
    assert {repos.observations.get("alice", f.passage.observation_id).run_id
            for f in result.facts} == {"run_a"}


# --- whole Runs ---------------------------------------------------------------------------

def test_concurrent_runs_of_one_user_on_the_same_urls_never_read_each_others_content(shared):
    repos, _, deps_for = shared
    barrier = threading.Barrier(2)
    clock = {"t": T0}

    def now():
        return clock["t"]

    runs: dict[str, Run] = {}
    deps = {tag: deps_for(Tagged(tag), barrier, now) for tag in ("Alpha", "Beta")}

    def run(tag: str) -> None:
        runs[tag] = run_brief(deps[tag], "alice", Brief(text=fakes.QUERIES["s1"]))

    threads = [threading.Thread(target=run, args=(t,)) for t in deps]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    shared_sources = ({s.id for s in repos.sources.list_for_run("alice", runs["Alpha"].id)}
                      & {s.id for s in repos.sources.list_for_run("alice", runs["Beta"].id)})
    assert shared_sources  # the same URLs: one Source each, with an Observation per Run
    for tag, d in deps.items():
        assert_reads_only_its_own(d, "alice", runs[tag], tag)


def test_runs_with_identical_content_and_timestamps_keep_their_own_provenance(shared):
    repos, _, deps_for = shared
    first = run_brief(deps_for(Tagged("Same")), "alice", Brief(text=fakes.QUERIES["s1"]))
    second = run_brief(deps_for(Tagged("Same")), "alice", Brief(text=fakes.QUERIES["s1"]))
    for run in (first, second):
        assert_reads_only_its_own(deps_for(Tagged("Same")), "alice", run, "Same")


def test_earlier_citations_resolve_to_their_original_passages_after_a_later_refetch(shared):
    repos, store, deps_for = shared
    clock = {"t": T0}
    alpha = deps_for(Tagged("Alpha"), now=lambda: clock["t"])
    first = run_brief(alpha, "alice", Brief(text=fakes.QUERIES["s1"]))
    report = repos.reports.get_for_run("alice", first.id)
    before = report_citations(store, "alice", report)
    clock["t"] = T0 + timedelta(days=1)
    later = run_brief(deps_for(Tagged("Beta"), now=lambda: clock["t"]), "alice",
                      Brief(text=fakes.QUERIES["s1"]))
    after = report_citations(store, "alice", repos.reports.get(report.owner_id, report.id))
    assert after == before  # same Observation, same Passage, same text
    assert all(c.passage_text.startswith("Alpha") for c in after)
    assert_reads_only_its_own(alpha, "alice", first, "Alpha")
    assert later.state == RunState.COMPLETE


def test_another_users_fetch_of_the_same_url_stays_isolated(shared):
    repos, store, deps_for = shared
    alice = run_brief(deps_for(Tagged("Alpha")), "alice", Brief(text=fakes.QUERIES["s1"]))
    bob = run_brief(deps_for(Tagged("Bob")), "bob", Brief(text=fakes.QUERIES["s1"]))
    assert_reads_only_its_own(deps_for(Tagged("Alpha")), "alice", alice, "Alpha")
    assert_reads_only_its_own(deps_for(Tagged("Bob")), "bob", bob, "Bob")
    sid = repos.sources.list_for_run("bob", bob.id)[0].id
    with pytest.raises(NotFoundError):
        store.read_run_source("alice", bob.id, sid) if sid not in {
            s.id for s in repos.sources.list_for_run("alice", alice.id)} else \
            repos.observations.get("alice", repos.observations.list_for_source("bob", sid)[0].id)
