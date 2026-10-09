"""Conflict detection over structured facts (design.md section 5, Extractor).

Facts are grouped by entity, predicate and scope. Two facts conflict when they give different
values for overlapping periods. A fact with an UNKNOWN period conflicts with nothing: it is
neither a change nor a contradiction. Raw text is never compared."""

import re
from collections import defaultdict

from insightforge_agent.domain.contracts import Conflict, ExtractedFact
from insightforge_agent.domain.periods import periods_overlap

_SPACE = re.compile(r"\s+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")


def normalise(text: str) -> str:
    """Case, spacing and thousands separators do not make two values different."""
    return _SPACE.sub(" ", _THOUSANDS.sub("", text)).strip().casefold()


def group_key(entity: str, predicate: str, scope: str) -> tuple[str, str, str]:
    return normalise(entity), normalise(predicate), normalise(scope)


def detect_conflicts(facts: list[ExtractedFact]) -> tuple[list[ExtractedFact], list[Conflict]]:
    """Returns the facts with `conflict` set where they take part in one, and the conflicts."""
    groups: dict[tuple[str, str, str], list[ExtractedFact]] = defaultdict(list)
    for f in facts:
        if f.entity.strip() and f.predicate.strip() and f.value.strip():
            groups[group_key(f.entity, f.predicate, f.scope)].append(f)

    conflicted: set[str] = set()
    conflicts: list[Conflict] = []
    for members in groups.values():
        involved: dict[str, ExtractedFact] = {}
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if (normalise(a.value) != normalise(b.value)
                        and periods_overlap(a.period, b.period)):
                    involved[a.id] = a
                    involved[b.id] = b
        if involved:
            first = next(iter(involved.values()))
            conflicts.append(Conflict(entity=first.entity, predicate=first.predicate,
                                      scope=first.scope, fact_ids=list(involved)))
            conflicted.update(involved)
    marked = [f.model_copy(update={"conflict": True}) if f.id in conflicted else f for f in facts]
    return marked, conflicts
