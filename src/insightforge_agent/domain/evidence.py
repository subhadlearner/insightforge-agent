"""Rules for Evidence items: Confidence, ranking and fitting the bundle to its budget.

Pure functions, no I/O. The pipeline supplies the facts (Sources, scores, token costs)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import urlparse

from insightforge_agent.domain.contracts import CONFIDENCE_ORDER, Confidence, EvidenceItem
from insightforge_agent.domain.periods import period_end


@dataclass(frozen=True)
class Thresholds:
    """Bucket boundaries for ranking and Confidence. They are settings."""

    credibility_high: float = 0.65
    credibility_medium: float = 0.4
    recency_fresh_days: int = 365
    recency_recent_days: int = 3 * 365


def credibility_bucket(score: float, t: Thresholds) -> int:
    """2 high, 1 medium, 0 low."""
    return 2 if score >= t.credibility_high else 1 if score >= t.credibility_medium else 0


def recency_bucket(as_of_period: str, today: date, t: Thresholds) -> int:
    """2 fresh, 1 recent, 0 older. An UNKNOWN period is never assumed recent."""
    end = period_end(as_of_period)
    if end is None:
        return 0
    age = (today - end).days
    return 2 if age <= t.recency_fresh_days else 1 if age <= t.recency_recent_days else 0


def confidence_of(
    *, has_independent_pair: bool, best_credibility: float, conflicting: bool, high: float,
) -> Confidence:
    """Counted over SUPPORTED Passages' distinct Sources only (design.md section 7)."""
    if conflicting:
        return "LOW"
    if has_independent_pair:
        return "HIGH"
    return "MEDIUM" if best_credibility >= high else "LOW"


DecisionRule = Literal["conflicting", "independent_sources", "high_credibility_single_source",
                       "insufficient_credibility"]


def decision_rule(
    *, has_independent_pair: bool, best_credibility: float, conflicting: bool, high: float,
) -> DecisionRule:
    """Which rule of `confidence_of` decided, in the same order it applies them."""
    if conflicting:
        return "conflicting"
    if has_independent_pair:
        return "independent_sources"
    return "high_credibility_single_source" if best_credibility >= high else "insufficient_credibility"


def section_label(confidences: list[Confidence]) -> Confidence:
    return min(confidences, key=CONFIDENCE_ORDER.__getitem__)


def rank_key(
    item: EvidenceItem, *, recency: int, credibility: int, priority: int,
) -> tuple[int, int, float, int, str]:
    """Ascending sort key for `(recency desc, credibility desc, relevance desc, priority asc,
    evidence_id asc)`. Relevance is rounded so float noise cannot reorder items."""
    return (-recency, -credibility, -round(item.relevance, 6), priority, item.id)


def compress_to_budget(
    ranked: list[EvidenceItem], fits: Callable[[list[EvidenceItem]], bool],
) -> tuple[list[EvidenceItem], list[EvidenceItem]]:
    """Cut the lowest-ranked items until `fits`. Whole items go, never half a statement, and an
    item that is the last of its Aspect goes only when nothing else can. Returns (kept, cut)."""
    kept, cut = list(ranked), []
    while kept and not fits(kept):
        counts: dict[str, int] = {}
        for i in kept:
            counts[i.aspect_id] = counts.get(i.aspect_id, 0) + 1
        victim = next((i for i in reversed(kept) if counts[i.aspect_id] > 1), kept[-1])
        kept.remove(victim)
        cut.append(victim)
    return kept, cut


def domain_of(locator: str) -> str:
    return (urlparse(locator).hostname or locator).lower().removeprefix("www.")


def cites(passage_texts: list[str], locator: str) -> bool:
    """Whether a Source's text links to another Source, by its address or its domain."""
    domain = domain_of(locator)
    return any(locator.lower() in t.lower() or domain in t.lower() for t in passage_texts)


def sources_independent(
    a_locator: str, a_texts: list[str], b_locator: str, b_texts: list[str],
) -> bool:
    """Two Sources are independent when they sit on different domains and neither's text links
    to the other, by address or domain. This is the predicate behind Confidence HIGH."""
    return (domain_of(a_locator) != domain_of(b_locator)
            and not cites(a_texts, b_locator) and not cites(b_texts, a_locator))
