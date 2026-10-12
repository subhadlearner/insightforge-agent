from types import SimpleNamespace

import pytest

from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    PassageRef,
    ReportDraft,
    ReportSection,
    VerifiedReport,
    ClaimVerdict,
)
from insightforge_agent.domain.models import (
    Report,
    Observation,
    Passage,
    Source,
    RunState,
)
from insightforge_agent.scheduler.watchlist import WatchlistScheduler
from tests.stores.test_watchlist_repository import NOW, item, run


def prepare(repos, foreign_observation=False, historical_only=False):
    repos.watchlists.add(item())
    repos.runs.add(run("first", RunState.COMPLETE))
    repos.sources.add(
        Source(
            id="s",
            owner_id="alice",
            run_id="first",
            kind="web",
            locator="https://example.com",
            title="Source",
        )
    )
    repos.observations.add(
        Observation(
            id="o",
            owner_id="alice",
            run_id="other" if foreign_observation else "first",
            source_id="s",
            fetched_at=NOW,
        )
    )
    repos.passages.add_all(
        "alice",
        "o",
        [Passage(observation_id="o", index=0, text="Example price is 100.")],
    )
    ref = PassageRef(observation_id="o", index=0)
    evidence = EvidenceItem(
        id="e",
        subtask_id="s1",
        aspect_id="a1",
        statement="Example price is 100.",
        confidence="MEDIUM",
        primary=ref,
        supporting=[ref],
        entity="Example",
        predicate="price",
        value="100",
        scope="US",
        as_of_period="2030",
    )
    claims = [
        Claim(
            text="Example price is 100.", evidence_id="e", historical=historical_only
        ),
        Claim(text="Historical claim", evidence_id="historical", historical=True),
        Claim(text="Unverified claim", evidence_id="e"),
    ]
    draft = ReportDraft(
        title="Report", sections=[ReportSection(heading="Pricing", claims=claims)]
    )
    verified = VerifiedReport(
        draft=draft,
        verdicts=[
            ClaimVerdict(
                section=0,
                claim=2,
                evidence_id="e",
                verdict="unverified",
                check="web_refetch",
                original=ref,
            )
        ],
    )
    report = Report(
        id="rep",
        owner_id="alice",
        run_id="first",
        created_at=NOW,
        body={
            "verified": verified.model_dump(mode="json"),
            "evidence": EvidenceBundle(
                sections=[
                    EvidenceSection(
                        aspect_id="a1",
                        heading="Pricing",
                        confidence="MEDIUM",
                        items=[evidence],
                    )
                ]
            ).model_dump(mode="json"),
        },
    )
    repos.reports.add(report)
    return WatchlistScheduler(SimpleNamespace(repos=repos), None, None)


def test_initialization_excludes_historical_and_unverified_and_keeps_structured_provenance(
    repos,
):
    service = prepare(repos)
    baseline = service.initialize("alice", "w")
    assert len(baseline.seeds) == 1
    seed = baseline.seeds[0]
    assert (
        seed.evidence.entity,
        seed.evidence.predicate,
        seed.evidence.value,
        seed.evidence.scope,
        seed.evidence.as_of_period,
    ) == ("Example", "price", "100", "US", "2030")
    assert seed.observation_dates == {"o": NOW}
    assert seed.source_ids == ["s"] and seed.report_id == "rep"
    assert service.initialize("alice", "w") == baseline
    assert repos.events.read_after("alice", "first") == []  # neither Diff nor Alert


def test_foreign_run_evidence_cannot_seed_baseline(repos):
    service = prepare(repos, foreign_observation=True)
    with pytest.raises(ValueError, match="establishing Run"):
        service.initialize("alice", "w")
    assert repos.watchlists.baseline("alice", "w") is None


def test_empty_current_input_is_distinct_from_no_success(repos):
    service = prepare(repos, historical_only=True)
    baseline = service.initialize("alice", "w")
    assert baseline is not None and baseline.seeds == []
    assert baseline.initializing_run_id == "first"
