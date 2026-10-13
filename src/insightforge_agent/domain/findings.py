"""Findings and the controlled predicate vocabulary (design.md section 8, ADR-0004, ADR-0005).

A Finding is a Claim normalised into a triple (subject, predicate, object) with a polarity, a
scope and an As-of period. The vocabulary is authoritative for cardinality and identity: a
single-valued predicate identifies a Finding by (subject, predicate, scope), a multi-valued one by
(subject, predicate, object, scope). The As-of period is never part of the identity.

What is deterministic here: alias lookup in the declared vocabulary, exact quantity comparison
(`Decimal`, via `quantities.canonical_value`), case/spacing normalisation (`extraction.normalise`)
and period arithmetic (`periods`). Anything semantic outside that (an out-of-vocabulary predicate,
an incomplete triple, or the polarity of a predicate the vocabulary marks as negatable in prose)
is a `JudgeRequest` for the narrow Finding judge, whose answer is checked here before it is used.
"""

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.domain.extraction import normalise, same_value
from insightforge_agent.domain.periods import UNKNOWN, stated_period
from insightforge_agent.domain.quantities import canonical_value
from insightforge_agent.domain.watchlist import BaselineSeed

Cardinality = Literal["single", "multi"]
ObjectType = Literal["quantity", "entity", "text"]
Polarity = Literal["affirmed", "negated"]
UNSUPPORTED = "unsupported"

_SLUG = re.compile(r"[^0-9a-z]+")


def slug(predicate: str) -> str:
    """`Starting Price`, `starting-price` and `starting_price` are one alias."""
    return _SLUG.sub("_", predicate.casefold()).strip("_")


class PredicateSpec(BaseModel):
    """One controlled predicate. `tolerance` is absolute, in the canonical unit, and zero unless
    declared: two quantities differ when their exact values are more than `tolerance` apart."""

    model_config = ConfigDict(frozen=True)

    name: str
    cardinality: Cardinality
    object_type: ObjectType
    aliases: frozenset[str] = frozenset()
    negated_aliases: frozenset[str] = frozenset()  # e.g. `discontinued` for `offers`
    tolerance: Decimal = Decimal(0)
    # Statements of this predicate can deny it in prose ("no longer offers X"), so their
    # polarity is judged by the model rather than assumed affirmed.
    polarity_judged: bool = False

    @model_validator(mode="after")
    def _valid(self):
        if self.tolerance < 0:
            raise ValueError("tolerance must not be negative")
        if self.tolerance and self.object_type != "quantity":
            raise ValueError("only a quantity predicate may declare a tolerance")
        if slug(self.name) != self.name:
            raise ValueError(f"predicate name {self.name!r} must be a slug")
        return self


class PredicateRegistry:
    """The controlled vocabulary. Every alias names exactly one predicate and polarity."""

    def __init__(self, specs: list[PredicateSpec]) -> None:
        self._specs = {s.name: s for s in specs}
        if len(self._specs) != len(specs):
            raise ValueError("duplicate predicate names")
        self._aliases: dict[str, tuple[PredicateSpec, Polarity]] = {}
        for spec in specs:
            for alias, polarity in [
                *((a, "affirmed") for a in {spec.name, *spec.aliases}),
                *((a, "negated") for a in spec.negated_aliases),
            ]:
                key = slug(alias)
                if key in self._aliases:
                    raise ValueError(f"alias {alias!r} is declared twice")
                self._aliases[key] = (spec, polarity)

    def resolve(self, predicate: str) -> tuple[PredicateSpec, Polarity] | None:
        return self._aliases.get(slug(predicate)) if predicate.strip() else None

    def get(self, name: str) -> PredicateSpec:
        return self._specs[name]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        return sorted(self._specs)

    def describe(self) -> str:
        """The vocabulary as the Finding judge sees it: one line per predicate."""
        return "\n".join(
            f"- {s.name} ({s.cardinality}-valued, object: {s.object_type})"
            for s in sorted(self._specs.values(), key=lambda s: s.name)
        )


def _spec(name, cardinality, object_type, *aliases, negated=(), judged=False):
    return PredicateSpec(
        name=name,
        cardinality=cardinality,
        object_type=object_type,
        aliases=frozenset(aliases),
        negated_aliases=frozenset(negated),
        polarity_judged=judged,
    )


# Market-intelligence predicates. Every tolerance is zero: a change is any exact difference.
DEFAULT_REGISTRY = PredicateRegistry([
    _spec("price", "single", "quantity", "starting_price", "base_price", "list_price",
          "msrp", "pricing", "monthly_price", "subscription_price"),
    _spec("revenue", "single", "quantity", "total_revenue", "annual_revenue"),
    _spec("units_sold", "single", "quantity", "unit_sales", "sales_volume", "vehicles_sold"),
    _spec("deliveries", "single", "quantity", "vehicle_deliveries", "units_delivered"),
    _spec("market_share", "single", "quantity", "share_of_market"),
    _spec("employee_count", "single", "quantity", "employees", "headcount"),
    _spec("valuation", "single", "quantity", "market_valuation"),
    _spec("ceo", "single", "entity", "chief_executive", "chief_executive_officer"),
    _spec("headquarters", "single", "entity", "headquartered_in", "hq"),
    _spec("offers", "multi", "entity", "sells", "offers_product", "has_product",
          negated=("discontinued", "no_longer_offers", "stopped_selling"), judged=True),
    _spec("competes_with", "multi", "entity", "competitor", "rival", judged=True),
    _spec("partners_with", "multi", "entity", "partner", "partnership", judged=True),
    _spec("operates_in", "multi", "entity", "available_in", "present_in",
          negated=("exited", "withdrew_from"), judged=True),
    _spec("acquired", "multi", "entity", "acquisition", "bought"),
])


class FindingProvenance(BaseModel):
    """What established a Finding: the Run, its Report, the Claim and Evidence item, and the
    Passages, Sources and Observation dates behind it. Stored with the Finding itself."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    report_id: str
    evidence_id: str
    claim: str
    statement: str
    primary: PassageRef
    supporting: list[PassageRef]
    source_ids: list[str]
    observation_dates: dict[str, datetime]

    def observed_at(self) -> datetime | None:
        """When the primary Passage was fetched (else the latest supporting fetch)."""
        return self.observation_dates.get(self.primary.observation_id) or max(
            self.observation_dates.values(), default=None
        )


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str  # the identity: never contains the As-of period
    subject: str
    predicate: str  # a name in the vocabulary
    object: str
    object_key: str  # canonical object: exact quantity key, or normalised text
    polarity: Polarity
    scope: str = ""
    as_of_period: str = UNKNOWN
    derived_from: str | None = None  # "observation_date" for a derived period
    conflicting: bool = False  # the Evidence item was conflicting within its own Run
    resolution: Literal["vocabulary", "judge"] = "vocabulary"
    provenance: FindingProvenance

    @property
    def derived(self) -> bool:
        return self.derived_from == "observation_date"


def object_key(spec: PredicateSpec, value: str) -> str | None:
    """A quantity predicate needs one exact quantity; anything else compares normalised."""
    if spec.object_type == "quantity":
        return canonical_value(value)
    key = normalise(value)
    return key or None


def finding_key(spec: PredicateSpec, subject: str, scope: str, obj_key: str) -> str:
    parts = [normalise(subject), spec.name, normalise(scope)]
    if spec.cardinality == "multi":
        parts.append(obj_key)
    return json.dumps(parts, ensure_ascii=False)


def same_object(spec: PredicateSpec, a: str, b: str) -> bool:
    """Equal after canonicalisation, or within the predicate's declared tolerance."""
    if a == b:
        return True
    if spec.object_type != "quantity" or not spec.tolerance:
        return False
    (va, ua), (vb, ub) = (k.split("|", 1) for k in (a, b))
    return ua == ub and abs(Decimal(va) - Decimal(vb)) <= spec.tolerance


class Skipped(BaseModel):
    """A current Claim that did not become a Finding, and why. Logged with the Diff."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    claim: str
    reason: str


class Rejected(BaseModel):
    """A judge answer that failed the checks: an interpretation that failed, not a Claim known
    to be unsupported. It fails normalisation (and so the attempt) instead of being skipped."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    reason: str


class JudgeRequest(BaseModel):
    """One Claim the Finding judge must read: why, and what the vocabulary already settled."""

    model_config = ConfigDict(frozen=True)

    index: int
    reason: Literal["out_of_vocabulary", "incomplete_triple", "polarity"]
    seed: BaselineSeed


class Judgment(BaseModel):
    """The judge's structured answer for one request. Checked by `finding_from_judgment`."""

    index: int
    predicate: str
    polarity: Polarity
    subject: str = ""
    object: str = ""


def _period(seed: BaselineSeed) -> tuple[str, str | None]:
    e = seed.evidence
    if e.temporally_ambiguous:
        return UNKNOWN, None
    period = stated_period(e.as_of_period)
    return period, (e.derived_from if period != UNKNOWN else None)


def _provenance(seed: BaselineSeed) -> FindingProvenance:
    e = seed.evidence
    return FindingProvenance(
        run_id=seed.run_id,
        report_id=seed.report_id,
        evidence_id=e.id,
        claim=seed.claim.text,
        statement=e.statement,
        primary=e.primary,
        supporting=list(dict.fromkeys([e.primary, *e.supporting])),
        source_ids=seed.source_ids,
        observation_dates=seed.observation_dates,
    )


def build_finding(
    seed: BaselineSeed,
    spec: PredicateSpec,
    subject: str,
    obj: str,
    polarity: Polarity,
    resolution: Literal["vocabulary", "judge"],
) -> Finding | Skipped:
    key = object_key(spec, obj)
    if key is None:
        return Skipped(evidence_id=seed.evidence.id, claim=seed.claim.text,
                       reason=f"object {obj!r} is not a single exact {spec.object_type}")
    period, derived_from = _period(seed)
    return Finding(
        key=finding_key(spec, subject, seed.evidence.scope, key),
        subject=subject.strip(),
        predicate=spec.name,
        object=obj.strip(),
        object_key=key,
        polarity=polarity,
        scope=seed.evidence.scope.strip(),
        as_of_period=period,
        derived_from=derived_from,
        conflicting=seed.evidence.conflicting,
        resolution=resolution,
        provenance=_provenance(seed),
    )


def eligibility_problem(seed: BaselineSeed, run_id: str) -> str | None:
    """Only current Claims of this Run, backed by this Run's fetches, may become Findings."""
    if seed.claim.historical:
        return "a Historical Claim never becomes a Finding"
    if seed.run_id != run_id:
        return "the Claim belongs to another Run"
    if not seed.observation_dates or not seed.source_ids:
        return "the Claim has no Source fetched in this Run"
    return None


def normalize_seed(
    seed: BaselineSeed, index: int, run_id: str, registry: PredicateRegistry
) -> Finding | Skipped | JudgeRequest:
    """The deterministic path: an in-vocabulary predicate with a complete triple. Everything
    else, and the polarity of a `polarity_judged` predicate, goes to the judge."""
    problem = eligibility_problem(seed, run_id)
    if problem:
        return Skipped(evidence_id=seed.evidence.id, claim=seed.claim.text, reason=problem)
    e = seed.evidence
    resolved = registry.resolve(e.predicate)
    if not (e.entity.strip() and e.predicate.strip() and e.value.strip()):
        return JudgeRequest(index=index, reason="incomplete_triple", seed=seed)
    if resolved is None:
        return JudgeRequest(index=index, reason="out_of_vocabulary", seed=seed)
    spec, polarity = resolved
    if spec.polarity_judged and polarity == "affirmed":
        return JudgeRequest(index=index, reason="polarity", seed=seed)
    return build_finding(seed, spec, e.entity, e.value, polarity, "vocabulary")


def _grounded(value: str, seed: BaselineSeed) -> bool:
    """A judge's subject or object must be stated by the Claim, its Evidence statement or its
    structured fields: a model proposes, code checks what it can."""
    text = normalise(f"{seed.claim.text} {seed.evidence.statement}")
    v = normalise(value)
    e = seed.evidence
    return bool(v) and (
        v in text or v in (normalise(e.entity), normalise(e.value))
        or (bool(e.value) and same_value(value, e.value))
    )


def finding_from_judgment(
    request: JudgeRequest, judgment: Judgment, registry: PredicateRegistry
) -> Finding | Skipped | Rejected:
    """Only an explicit `unsupported` (or an inexact quantity) skips the Claim; an answer
    that fails the checks is Rejected, so the Claim is never silently dropped."""
    seed = request.seed
    e = seed.evidence

    def skip(reason: str) -> Skipped:
        return Skipped(evidence_id=e.id, claim=seed.claim.text, reason=reason)

    def reject(reason: str) -> Rejected:
        return Rejected(evidence_id=e.id, reason=reason)

    if request.reason == "polarity":
        # The vocabulary already fixed the triple; the judge only reads its polarity, and must
        # echo that triple back (`fixed_triple`). Any other answer is a failed interpretation.
        spec, _ = registry.resolve(e.predicate)
        if (judgment.predicate != spec.name
                or normalise(judgment.subject) != normalise(e.entity)
                or normalise(judgment.object) != normalise(e.value)):
            return reject("a polarity answer must repeat the fixed predicate, subject and "
                          f"object ({spec.name}, {e.entity!r}, {e.value!r})")
        return build_finding(seed, spec, e.entity, e.value, judgment.polarity, "judge")
    if judgment.predicate == UNSUPPORTED:
        return skip(f"{request.reason}: the judge found no controlled predicate")
    if judgment.predicate not in registry:
        return reject(f"the judge answered an unknown predicate {judgment.predicate!r}")
    if not _grounded(judgment.subject, seed) or not _grounded(judgment.object, seed):
        return reject("the judge's subject or object is not stated by the Claim")
    return build_finding(seed, registry.get(judgment.predicate), judgment.subject,
                         judgment.object, judgment.polarity, "judge")


class NormalizedRun(BaseModel):
    """A Run's current Claims as Findings, plus the Claims that did not qualify."""

    findings: list[Finding] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
