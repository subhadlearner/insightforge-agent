"""Small builders for T12 Finding and Diff tests (no I/O)."""

from datetime import UTC, datetime

from insightforge_agent.domain.contracts import Claim, EvidenceItem, PassageRef
from insightforge_agent.domain.findings import DEFAULT_REGISTRY, Finding, build_finding
from insightforge_agent.domain.watchlist import BaselineSeed

T = datetime(2030, 3, 1, 9, tzinfo=UTC)


def seed(
    *,
    entity="Acme",
    predicate="price",
    value="$100",
    scope="US",
    period="2030-02",
    derived_from=None,
    run_id="r1",
    report_id="rep1",
    evidence_id="e1",
    statement=None,
    claim=None,
    historical=False,
    conflicting=False,
    temporally_ambiguous=False,
    observed=T,
    observation="o1",
) -> BaselineSeed:
    ref = PassageRef(observation_id=observation, index=0)
    statement = statement or f"{entity} {predicate} is {value}."
    return BaselineSeed(
        claim=Claim(text=claim or statement, evidence_id=evidence_id, historical=historical),
        evidence=EvidenceItem(
            id=evidence_id, subtask_id="s1", aspect_id="a1", statement=statement,
            confidence="MEDIUM", primary=ref, supporting=[ref], entity=entity,
            predicate=predicate, value=value, scope=scope, as_of_period=period,
            derived_from=derived_from, conflicting=conflicting,
            temporally_ambiguous=temporally_ambiguous,
        ),
        observation_dates={observation: observed},
        source_ids=["src1"],
        report_id=report_id,
        run_id=run_id,
    )


def finding(polarity="affirmed", registry=DEFAULT_REGISTRY, **kw) -> Finding:
    s = seed(**kw)
    spec, _ = registry.resolve(s.evidence.predicate)
    result = build_finding(s, spec, s.evidence.entity, s.evidence.value, polarity, "vocabulary")
    assert isinstance(result, Finding), result
    return result
