"""Rules for Evidence items: Confidence, ranking and fitting the bundle to its budget.

Pure functions, no I/O. The pipeline supplies the facts (Sources, scores, token costs)."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import urlparse

from insightforge_agent.domain.contracts import CONFIDENCE_ORDER, Confidence, EvidenceItem
from insightforge_agent.domain.periods import period_end
from insightforge_agent.domain.tokens import estimate_tokens

# Why a drafted candidate was not kept: logged by Synthesis, counted by the Report's gap lines.
DROP_NO_CITATION = "draft cites no Passage"
DROP_LITERAL = "no Passage passes the provenance and literal-value checks"
DROP_UNSUPPORTED = "no supported Passage"
# PR #77 review: no Passage was judged SUPPORTED and at least one that could support the item
# was never judged (every reply for it unusable): not the same as insufficient Evidence.
DROP_UNVERIFIED = "verification incomplete: the support check failed"


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


def summary_text(item: EvidenceItem) -> str:
    """An item's structured fact, as a compressed item is shown to the Writer: subject,
    relation and value (which Synthesis kept only if a supporting Passage states it), and its
    scope. The period is shown beside it, as for every item."""
    scope = f" ({item.scope})" if item.scope else ""
    return f"{item.entity}: {item.predicate.replace('_', ' ')} {item.value}{scope}"


def summarised(item: EvidenceItem) -> EvidenceItem | None:
    """The item compressed to its structured fact, or None when it has none (no entity,
    relation and stated value) or the fact is not shorter than its statement."""
    if item.compressed or not (item.entity and item.predicate and item.value):
        return None
    if estimate_tokens(summary_text(item)) >= estimate_tokens(item.statement):
        return None
    return item.model_copy(update={"compressed": True})


def compress_to_budget(
    ranked: list[EvidenceItem], fits: Callable[[list[EvidenceItem]], bool],
    essential: Callable[[EvidenceItem, list[EvidenceItem]], bool] = lambda item, kept: False,
    shorten: Callable[[EvidenceItem], EvidenceItem | None] = summarised,
) -> tuple[list[EvidenceItem], list[EvidenceItem], list[EvidenceItem]]:
    """Fit the bundle to the budget the way the assignment asks, by compressing lower-priority
    items before cutting any. First, lowest-ranked first, each item that has a structured fact
    is summarised to it (`shorten`): same id and citation, so provenance is unchanged. Only if
    that is not enough are the lowest-ranked items cut, whole, never half a statement. An item
    that is `essential` (ADR-0006: the last kept support of a required requirement) goes only
    when nothing else can, and an item that is the last of its Aspect only after every other
    non-essential one. Returns (kept, cut, summarised)."""
    kept, cut, short = list(ranked), [], []
    for n in range(len(kept) - 1, -1, -1):
        if fits(kept):
            break
        if (s := shorten(kept[n])) is not None:
            kept[n] = s
            short.append(s)
    while kept and not fits(kept):
        counts: dict[str, int] = {}
        for i in kept:
            counts[i.aspect_id] = counts.get(i.aspect_id, 0) + 1
        spare = [i for i in reversed(kept) if not essential(i, kept)]
        victim = next((i for i in spare if counts[i.aspect_id] > 1),
                      spare[0] if spare else kept[-1])
        kept.remove(victim)
        cut.append(victim)
    return kept, cut, [s for s in short if any(s is k for k in kept)]


def domain_of(locator: str) -> str:
    return (urlparse(locator).hostname or locator).lower().removeprefix("www.")


def cites(passage_texts: list[str], locator: str) -> bool:
    """Whether a Source's text links to another Source, by its address or its domain."""
    domain = domain_of(locator)
    return any(locator.lower() in t.lower() or domain in t.lower() for t in passage_texts)


_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def syndicated(a_texts: list[str], b_texts: list[str], *, threshold: float = 0.8,
               minimum: int = 12) -> bool:
    """Whether two Sources' supporting Passages are the same text (a syndicated story, a
    re-posted press release): word-set overlap of at least `threshold` between some pair of
    Passages of at least `minimum` words. Such copies share one upstream source, so different
    domains do not make them independent (ADR-0006 §3, Confidence)."""
    def words(t: str) -> set[str]:
        return {w.casefold() for w in _WORDS.findall(t)}

    for a in a_texts:
        wa = words(a)
        for b in b_texts:
            wb = words(b)
            if min(len(wa), len(wb)) >= minimum and len(wa & wb) / len(wa | wb) >= threshold:
                return True
    return False


def sources_independent(
    a_locator: str, a_texts: list[str], b_locator: str, b_texts: list[str],
) -> bool:
    """Two Sources are independent when they sit on different domains and neither's text links
    to the other, by address or domain. This is the predicate behind Confidence HIGH."""
    return (domain_of(a_locator) != domain_of(b_locator)
            and not cites(a_texts, b_locator) and not cites(b_texts, a_locator))
