"""The Diff: a Run's Findings against the latest established state of each Finding (ADR-0004).

Pure and deterministic. Inputs are the established Findings by identity and the Run's
normalised Findings; the output is the Diff log entries and the new state of every Finding the
Run touched. Never the preceding Report: what is compared is per-Finding state.

Outcomes, per identity:

- `added`: no established Finding, or the established one had the opposite polarity and this
  Run affirms it again (material).
- `changed`: a single-valued object differs beyond tolerance, from a newer or equal-newest
  period (material).
- `no_longer_true`: an explicit negated Finding of the established value (material). A fresh
  replacement value of a single-valued predicate is `changed`, which also retires the old value.
- `reconfirmed`: the same value and polarity, not older. Resets the Not reconfirmed count and
  refreshes the establishing provenance (and period).
- `not_reconfirmed`: an established Finding this Run neither confirmed nor contradicted. Absence
  is never deletion: the value is kept and the consecutive count grows; at K it is unconfirmed,
  still without an Alert.
- `older_ignored`: a value whose As-of period ends before the established one's. Kept for audit,
  never replaces the newer established value.
- `ambiguous`: an UNKNOWN period, a different value for the same period, or a conflicting value
  within this Run. Flagged; neither a change nor a contradiction, never establishes.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from insightforge_agent.domain.findings import Finding, PredicateRegistry, same_object
from insightforge_agent.domain.periods import period_end

Outcome = Literal[
    "added", "changed", "no_longer_true", "reconfirmed", "not_reconfirmed",
    "older_ignored", "ambiguous",
]
MATERIAL: frozenset[str] = frozenset({"added", "changed", "no_longer_true"})
DEFAULT_UNCONFIRMED_AFTER = 3  # K: consecutive Not reconfirmed successful Diffs


class EstablishedFinding(BaseModel):
    """The latest established state of one Finding, with the provenance that established it."""

    model_config = ConfigDict(frozen=True)

    finding: Finding
    not_reconfirmed: int = 0  # consecutive successful Diffs that neither confirmed nor refuted
    unconfirmed: bool = False  # not_reconfirmed reached K; shown, never alerted, never removed


class DiffEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    key: str
    finding: Finding | None = None  # this Run's Finding
    previous: Finding | None = None  # the established Finding it was compared with
    not_reconfirmed: int | None = None
    unconfirmed: bool = False
    reason: str = ""

    @property
    def material(self) -> bool:
        return self.outcome in MATERIAL


class DiffResult(BaseModel):
    entries: list[DiffEntry] = Field(default_factory=list)
    updates: dict[str, EstablishedFinding] = Field(default_factory=dict)

    @property
    def material(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.material]


def temporal_order(new: Finding, old: Finding) -> Literal["newer", "older", "same"] | None:
    """By the end of each As-of period. Two derived periods on the same day are ordered by when
    they were observed, since a derived date says only when the state was seen. None when
    either period is UNKNOWN: an unknown period is never assumed to match or precede another."""
    a, b = period_end(new.as_of_period), period_end(old.as_of_period)
    if a is None or b is None:
        return None
    if a != b:
        return "newer" if a > b else "older"
    if new.derived and old.derived:
        ta, tb = new.provenance.observed_at(), old.provenance.observed_at()
        if ta and tb and ta != tb:
            return "newer" if ta > tb else "older"
    return "same"


def _newest(group: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """The Findings tied for the newest period, and the older ones (the group is all dated)."""
    newest: list[Finding] = []
    older: list[Finding] = []
    for f in group:
        order = temporal_order(f, newest[0]) if newest else "newer"
        if order == "newer":
            older.extend(newest)
            newest = [f]
        elif order == "same":
            newest.append(f)
        else:
            older.append(f)
    return newest, older


def compare(
    established: dict[str, EstablishedFinding],
    findings: list[Finding],
    registry: PredicateRegistry,
    unconfirmed_after: int = DEFAULT_UNCONFIRMED_AFTER,
) -> DiffResult:
    entries: list[DiffEntry] = []
    updates: dict[str, EstablishedFinding] = {}
    touched: set[str] = set()
    reasons: dict[str, str] = {}

    groups: dict[str, list[Finding]] = {}
    for f in findings:
        groups.setdefault(f.key, []).append(f)

    for key, group in groups.items():
        current = established.get(key)
        previous = current.finding if current else None
        dated = []
        for f in group:
            if period_end(f.as_of_period) is None:
                entries.append(DiffEntry(outcome="ambiguous", key=key, finding=f,
                                         previous=previous, reason="UNKNOWN As-of period"))
            else:
                dated.append(f)
        if not dated:
            continue
        newest, older = _newest(dated)
        for f in older:
            entries.append(DiffEntry(outcome="older_ignored", key=key, finding=f,
                                     previous=previous,
                                     reason="older than another value in this Run"))
        if len({(f.object_key, f.polarity) for f in newest}) > 1 or any(
            f.conflicting for f in newest
        ):
            for f in newest:
                entries.append(DiffEntry(outcome="ambiguous", key=key, finding=f,
                                         previous=previous,
                                         reason="conflicting values in this Run"))
            continue
        new = newest[0]

        def settle(outcome: Outcome, reason: str = "") -> None:
            entries.append(DiffEntry(outcome=outcome, key=key, finding=new, previous=previous,
                                     reason=reason))
            updates[key] = EstablishedFinding(finding=new)
            touched.add(key)

        if current is None:
            settle("added")
            continue
        order = temporal_order(new, previous)
        if order == "older":
            entries.append(DiffEntry(outcome="older_ignored", key=key, finding=new,
                                     previous=previous,
                                     reason="older than the established value"))
            continue
        spec = registry.get(new.predicate)
        same = same_object(spec, new.object_key, previous.object_key)
        if new.polarity == previous.polarity:
            if same:
                settle("reconfirmed")
            elif new.polarity == "negated":
                reasons[key] = "negates a different value than the established one"
            elif order == "same":
                entries.append(DiffEntry(outcome="ambiguous", key=key, finding=new,
                                         previous=previous,
                                         reason="a different value for the established period"))
            else:
                settle("changed")
        elif new.polarity == "negated":
            if same:
                settle("no_longer_true")
            else:
                reasons[key] = "negates a different value than the established one"
        else:  # affirmed again after an established negation
            settle("added" if same else "changed")

    for key, current in established.items():
        if key in touched:
            continue
        count = current.not_reconfirmed + 1
        unconfirmed = count >= unconfirmed_after
        updates[key] = current.model_copy(
            update={"not_reconfirmed": count, "unconfirmed": unconfirmed}
        )
        entries.append(DiffEntry(outcome="not_reconfirmed", key=key, previous=current.finding,
                                 not_reconfirmed=count, unconfirmed=unconfirmed,
                                 reason=reasons.get(key, "")))
    return DiffResult(entries=entries, updates=updates)


def establish(findings: list[Finding], registry: PredicateRegistry) -> DiffResult:
    """The seed-to-established transition: the first successful Run's Findings, without an
    Alert. The same rules apply (UNKNOWN, conflicting and older values never establish)."""
    return compare({}, findings, registry)
