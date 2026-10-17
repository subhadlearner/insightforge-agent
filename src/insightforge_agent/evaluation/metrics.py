"""The deterministic metrics of a stored Report. No model is called.

**Fact-check pass rate** is the Fact-Checker's own result, read from the persisted
`body["verified"]` (design.md section 7): `verified / (verified + unverified)` over the sampled
Claims, with `unchecked` (the re-fetch failed) left out. It is None, never zero, when no
sampled Claim could be checked. Pooled over Reports it counts every checked Claim once, so a
Report with many checked Claims weighs more than one with few.

**Citation accuracy** covers every Claim, sampled or not. A Claim's citation is accurate when
it resolves along the production path (Claim -> Evidence or Historical item -> primary Passage
-> Source, `pipeline.citations`) and that primary Passage states every figure of the Claim
(`unsupported_numbers`, the check the Fact-Checker applies to its spans). A Historical Claim's
report-date annotation is not one of its figures. A Claim that does not resolve stays in the
denominator. This is provenance and figure grounding, not semantic support: whether the
Passage actually says what the Claim says is what the fact-check measures, on its sample."""

from datetime import date

from pydantic import BaseModel, ValidationError, computed_field

from insightforge_agent.domain.contracts import ClaimVerdict, HistoricalContext
from insightforge_agent.domain.errors import ProvenanceError
from insightforge_agent.domain.memory import without_report_date
from insightforge_agent.domain.models import Report
from insightforge_agent.domain.quantities import unsupported_numbers
from insightforge_agent.pipeline.citations import claim_citations
from insightforge_agent.stores.source_store import SourceStore

MAX_PROBLEMS = 20  # problem lines kept per Report; the counts are always complete

_KIND = {"web": "web", "pdf": "document"}


class MetricError(ValueError):
    """A Report whose stored state cannot be measured; it is reported, never scored."""


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


class FactCheckMetrics(BaseModel):
    claims: int = 0
    sampled: int = 0
    verified: int = 0
    unverified: int = 0
    unchecked: int = 0

    @computed_field
    @property
    def checked(self) -> int:
        return self.verified + self.unverified

    @computed_field
    @property
    def pass_rate(self) -> float | None:
        return _rate(self.verified, self.checked)


def fact_check_metrics(report: Report) -> FactCheckMetrics:
    """The persisted fact-check counts, cross-checked against the persisted verdicts."""
    try:
        verified = report.body["verified"]
        summary = verified["summary"]
        verdicts = [ClaimVerdict.model_validate(v) for v in verified["verdicts"]]
        m = FactCheckMetrics(claims=summary["claims"], sampled=summary["sampled"],
                             verified=summary["verified"], unverified=summary["unverified"],
                             unchecked=summary["unchecked"])
    except (KeyError, TypeError, ValidationError) as e:
        raise MetricError(f"report {report.id}: no usable fact-check result: {e!r}") from e
    counted = {k: sum(v.verdict == k for v in verdicts)
               for k in ("verified", "unverified", "unchecked")}
    stated = {"verified": m.verified, "unverified": m.unverified, "unchecked": m.unchecked}
    if counted != stated or m.sampled != len(verdicts):
        raise MetricError(f"report {report.id}: the fact-check summary {stated} (sampled "
                          f"{m.sampled}) disagrees with its {len(verdicts)} verdicts {counted}")
    return m


def pool_fact_checks(metrics: list[FactCheckMetrics]) -> FactCheckMetrics:
    return FactCheckMetrics(**{k: sum(getattr(m, k) for m in metrics)
                               for k in FactCheckMetrics.model_fields})


class KindCounts(BaseModel):
    claims: int = 0
    accurate: int = 0


class CitationMetrics(BaseModel):
    claims: int = 0
    accurate: int = 0
    unresolved: int = 0
    figures_not_in_passage: int = 0
    # "web", "document" or "historical"; a current Claim that does not resolve is "unknown".
    by_kind: dict[str, KindCounts] = {}
    problems: list[str] = []

    @computed_field
    @property
    def accuracy(self) -> float | None:
        return _rate(self.accurate, self.claims)


def citation_metrics(store: SourceStore, owner_id: str, report: Report) -> CitationMetrics:
    try:
        resolved = claim_citations(store, owner_id, report)
        dates: dict[str, date] = {h.id: h.report_date for h in HistoricalContext.model_validate(
            report.body.get("historical") or {}).items}
    except (ProvenanceError, ValidationError) as e:
        raise MetricError(f"report {report.id}: citations cannot be measured: {e}") from e
    m = CitationMetrics()

    def note(problem: str) -> None:
        if len(m.problems) < MAX_PROBLEMS:
            m.problems.append(problem)

    for found in resolved:
        c, where = found.text, f"claim {found.section}.{found.claim}"
        m.claims += 1
        if isinstance(found.citation, ProvenanceError):
            kind = "historical" if c.historical else "unknown"
            m.unresolved += 1
            note(f"{where}: unresolved: {found.citation}")
            accurate = False
        else:
            if c.historical:
                kind = "historical"
                text = without_report_date(c.text, dates[c.evidence_id])
            else:
                kind = _KIND[store.source_for_passage(owner_id, found.citation.passage).kind]
                text = c.text
            missing = unsupported_numbers(text, found.citation.passage_text)
            accurate = not missing
            if missing:
                m.figures_not_in_passage += 1
                note(f"{where}: figures {sorted(missing)} are not in the cited Passage")
        counts = m.by_kind.setdefault(kind, KindCounts())
        counts.claims += 1
        if accurate:
            m.accurate += 1
            counts.accurate += 1
    return m


def pool_citations(metrics: list[CitationMetrics]) -> CitationMetrics:
    pooled = CitationMetrics()
    for m in metrics:
        pooled.claims += m.claims
        pooled.accurate += m.accurate
        pooled.unresolved += m.unresolved
        pooled.figures_not_in_passage += m.figures_not_in_passage
        for kind, counts in m.by_kind.items():
            into = pooled.by_kind.setdefault(kind, KindCounts())
            into.claims += counts.claims
            into.accurate += counts.accurate
    return pooled
