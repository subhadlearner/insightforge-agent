"""Watchlist configuration and the durable first-success Baseline input (T11)."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from insightforge_agent.domain.contracts import Brief, Claim, EvidenceItem
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.models import Run


class WatchlistConflict(Exception):
    """A stale edit/tick, active Run, or exhausted limit; no new Run was stored."""


class InvalidBaselineCandidate(ValueError):
    """A successful Report whose own stored data cannot seed a Baseline: malformed body,
    missing Evidence or provenance rows, foreign Observations or non-web Sources. Only this
    error quarantines a candidate; any other failure is retried."""


def check_watchlist_sources(brief: Brief) -> None:
    if not brief.source_toggles.get("web"):
        raise SubmissionRejected("Watchlist Items require web search")
    if brief.source_toggles.get("documents") or brief.document_ids:
        raise SubmissionRejected("Watchlist Items cannot use uploaded documents")


def enabled_sources(toggles) -> set[str]:
    """Toggles compared by meaning: a false toggle and an omitted one are the same."""
    return {source for source, on in toggles.items() if on}


def check_watchlist_run_brief(item: "WatchlistItem", brief: Brief) -> None:
    """A Watchlist Run researches exactly the item's standing Brief and source choices."""
    check_watchlist_sources(brief)
    if brief.text != item.brief:
        raise SubmissionRejected("a Watchlist Run must use the item's standing Brief")
    if enabled_sources(brief.source_toggles) != enabled_sources(item.source_toggles):
        raise SubmissionRejected(
            "a Watchlist Run must use the item's configured source choices"
        )


class WatchlistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    brief: str = Field(min_length=1, max_length=10000)
    cadence_minutes: int = Field(gt=0, le=525600, strict=True)
    max_runs: int | None = Field(default=None, gt=0, strict=True)
    source_toggles: dict[Literal["web", "documents", "memory"], bool] = Field(
        default_factory=lambda: {"web": True, "documents": False, "memory": False}
    )

    @model_validator(mode="after")
    def valid(self):
        if not self.brief.strip():
            raise ValueError("Brief must not be blank")
        check_watchlist_sources(self.as_brief())
        if self.cadence_minutes < 15 and self.max_runs is None:
            raise ValueError("a cadence below 15 minutes requires max_runs")
        return self

    def as_brief(self) -> Brief:
        return Brief(text=self.brief, source_toggles=self.source_toggles)


class _WatchlistPatchBase(BaseModel):
    """Optimistic, partial Watchlist update supplied by an API client."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def has_changes(self):
        changed = self.model_fields_set - {"revision"}
        if not changed:
            raise ValueError("PATCH must include at least one Watchlist field")
        null_fields = changed.intersection({"brief", "cadence_minutes", "source_toggles"})
        if any(getattr(self, name) is None for name in null_fields):
            raise ValueError("only max_runs may be cleared with null")
        return self

    def changes(self) -> dict:
        return self.model_dump(exclude={"revision"}, exclude_unset=True)


# The PATCH contract has exactly the configuration fields and constraints, made optional.
# Building it from WatchlistConfig prevents create/PATCH validation from drifting apart.
WatchlistPatch = create_model(
    "WatchlistPatch",
    __base__=_WatchlistPatchBase,
    **{
        name: (
            Annotated[field.annotation | None, *field.metadata]
            if field.metadata
            else field.annotation | None,
            None,
        )
        for name, field in WatchlistConfig.model_fields.items()
    },
)


class WatchlistItem(WatchlistConfig):
    id: str
    owner_id: str
    created_at: datetime
    revision: int = 1
    next_tick_at: datetime

    def after(self, now: datetime) -> datetime:
        """Next anchored tick strictly after now, without replaying missed ticks."""
        step = timedelta(minutes=self.cadence_minutes)
        return self.next_tick_at + max(0, (now - self.next_tick_at) // step + 1) * step


def check_watchlist_brief_change(
    old: WatchlistItem, new: WatchlistItem | WatchlistConfig, runs_started: int
) -> None:
    """The transaction-safe immutable-Brief rule shared by service and repositories."""
    if old.brief != new.brief and runs_started:
        raise WatchlistConflict(
            "create a new Watchlist Item to change a Brief after Runs started"
        )


class BaselineSeed(BaseModel):
    """Current Claim + structured Evidence, awaiting T12's Finding normalizer.

    Never assign guessed predicate cardinality, polarity or Finding identity in T11.
    The complete provenance is persisted, independently of a Report pointer.
    """

    claim: Claim
    evidence: EvidenceItem
    observation_dates: dict[str, datetime]
    source_ids: list[str]
    report_id: str
    run_id: str


class Baseline(BaseModel):
    owner_id: str
    watchlist_item_id: str
    initialized_at: datetime
    initializing_run_id: str
    normalization_pending: Literal[True] = True
    seeds: list[BaselineSeed]


# Quarantines are scoped to the validator/schema that rejected a candidate. Incrementing
# this version makes older candidates eligible for re-evaluation after compatibility fixes.
BASELINE_SEED_SCHEMA_VERSION = 1


class WatchlistDashboard(BaseModel):
    item: WatchlistItem
    runs_started: int
    completed_runs: int
    failed_runs: int
    recent_failed_runs: list[Run]
    baseline: Baseline | None = None
