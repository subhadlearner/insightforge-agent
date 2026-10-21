"""T13 (#14) against T11/T12: a retention sweep between Watchlist Runs never breaks Baseline
initialization, seed normalisation, later Diffs, Alerts or the provenance they hold
(SQLite file, scripted judge)."""

from datetime import timedelta

import pytest

from insightforge_agent.domain.models import Observation, Passage, Source
from insightforge_agent.scheduler.retention import RetentionSweeper
from insightforge_agent.stores.source_store import SourceStore
from tests.stores.diff_world import ITEM, NOW, OWNER, World, fact

LATER = timedelta(days=45)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "t13.db")
    w.add_item()
    yield w
    w.repos.close()


def sweep(world):
    return RetentionSweeper(world.repos.retention, None, lambda: world.now, retention_days=30).sweep()


def add_uncited_page(world, run_id):
    """A page the Run fetched but no Claim cites."""
    sid, oid = f"src_{run_id}_extra", f"obs_{run_id}_extra"
    world.repos.sources.add(Source(id=sid, owner_id=OWNER, run_id=run_id, kind="web",
                                   locator=f"https://example.com/{sid}", title=sid))
    world.repos.observations.add(Observation(id=oid, owner_id=OWNER, source_id=sid,
                                             run_id=run_id, fetched_at=NOW))
    world.repos.passages.add_all(OWNER, oid, [Passage(observation_id=oid, index=0,
                                                      text="unrelated")])
    return oid


def resolvable(world, ref) -> bool:
    store = SourceStore(world.repos.sources, world.repos.observations, world.repos.passages)
    store.citation(OWNER, ref)
    return True


def test_a_baseline_seeded_from_an_expired_report_after_a_sweep(world):
    world.complete("r1", 1, fact())
    extra = add_uncited_page(world, "r1")
    world.now = NOW + LATER
    sweep(world)
    assert not world.sql("SELECT 1 FROM observations WHERE id=?", extra)

    # T11 initialization reads the Report's Evidence, Observations and Passages: all kept.
    assert world.seed() is not None
    seed = world.repos.watchlists.baseline(OWNER, ITEM).seeds[0]
    assert resolvable(world, seed.evidence.primary)


def test_diffs_and_alerts_after_sweeps_keep_both_findings_provenance(world):
    world.complete("r1", 1, fact("$100"))
    world.seed()
    world.complete("r2", 2, fact("$100"))
    world.process()  # established, r2 reconfirms
    add_uncited_page(world, "r1")
    add_uncited_page(world, "r2")

    world.now = NOW + LATER
    assert sweep(world)["observations"] == 2  # only the uncited pages went

    # A Run long after: its value changed. The Diff compares with stored state only.
    world.complete("r3", int(LATER.total_seconds() // 60) + 1, fact("$90", period="2030-02"),
                   fetched=world.now)
    world.process()
    (alert,) = world.alerts()
    (change,) = alert.changes
    assert (change.object, change.previous_object) == ("$90", "$100")
    entry = next(e for e in world.diff("r3").entries if e.outcome == "changed")
    assert resolvable(world, entry.finding.provenance.primary)
    assert resolvable(world, entry.previous.provenance.primary)

    # Every later sweep keeps all of it, including the provenance the Diff log alone names.
    world.now = NOW + 3 * LATER
    sweep(world)
    for e in world.diff("r3").entries:
        for f in (e.finding, e.previous):
            if f is not None:
                assert resolvable(world, f.provenance.primary)
    for state in world.findings()[1].values():
        assert resolvable(world, state.finding.provenance.primary)
    counts = world.repos.watchlists.counts(OWNER, ITEM)
    assert counts == (3, 3, 0)  # Watchlist counting is untouched
