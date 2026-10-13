from types import SimpleNamespace

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
from insightforge_agent.domain.watchlist import BASELINE_SEED_SCHEMA_VERSION
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
    return WatchlistScheduler(SimpleNamespace(repos=repos, now=lambda: NOW), None, None)


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
    assert service.initialize("alice", "w") is None
    assert repos.watchlists.baseline("alice", "w") is None
    assert (
        repos.watchlists.baseline_candidates(
            "alice", "w", BASELINE_SEED_SCHEMA_VERSION
        )
        == []
    )
    # A validator/schema upgrade makes the candidate eligible for re-evaluation.
    assert [
        report.run_id
        for report in repos.watchlists.baseline_candidates(
            "alice", "w", BASELINE_SEED_SCHEMA_VERSION + 1
        )
    ] == ["first"]


def test_any_non_temporary_candidate_error_is_quarantined_and_next_success_initializes(
    repos, monkeypatch
):
    service = prepare(repos, foreign_observation=True)
    repos.runs.add(
        run("second", RunState.COMPLETE).model_copy(
            update={"created_at": NOW.replace(second=1)}
        )
    )
    first = repos.reports.get("alice", "rep")
    verified = VerifiedReport.model_validate(first.body["verified"])
    historical = verified.model_copy(
        update={
            "draft": verified.draft.model_copy(
                update={
                    "sections": [
                        section.model_copy(
                            update={
                                "claims": [
                                    claim.model_copy(update={"historical": True})
                                    for claim in section.claims
                                ]
                            }
                        )
                        for section in verified.draft.sections
                    ]
                }
            )
        }
    )
    repos.reports.add(
        Report(
            id="rep-second",
            owner_id="alice",
            run_id="second",
            created_at=NOW.replace(second=1),
            body={
                "verified": historical.model_dump(mode="json"),
                "evidence": first.body["evidence"],
            },
        )
    )

    convert = service._baseline_from_report

    def malformed(owner_id, item_id, report):
        if report.run_id == "first":
            raise TypeError("malformed Report body")
        return convert(owner_id, item_id, report)

    monkeypatch.setattr(service, "_baseline_from_report", malformed)
    baseline = service.initialize("alice", "w")
    assert baseline.initializing_run_id == "second"
    assert baseline.seeds == []
    assert service.initialize("alice", "w") == baseline


def test_empty_current_input_is_distinct_from_no_success(repos):
    service = prepare(repos, historical_only=True)
    baseline = service.initialize("alice", "w")
    assert baseline is not None and baseline.seeds == []
    assert baseline.initializing_run_id == "first"
