"""The Diff log, Alerts and webhook delivery records (design.md section 8). Plain data, no I/O."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from insightforge_agent.domain.diff import DiffEntry, MaterialOutcome
from insightforge_agent.domain.findings import Finding, Polarity, Skipped

DiffStatus = Literal["pending", "succeeded", "failed"]
MAX_DIFF_ATTEMPTS = 2  # the first attempt and one retry


class FailedDiff(BaseModel):
    """An intervening Run whose Diff failed permanently; the next success notes it."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    report_id: str | None
    error: str


class DiffRecord(BaseModel):
    """One Run's Diff log entry. A succeeded record holds the comparison; a failed one only
    its error, and the Baseline was left unchanged."""

    owner_id: str
    watchlist_item_id: str
    run_id: str
    report_id: str | None
    status: DiffStatus
    attempts: int = 0
    error: str = ""
    entries: list[DiffEntry] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
    intervening_failed: list[FailedDiff] = Field(default_factory=list)
    alert_id: str | None = None
    updated_at: datetime


class AlertChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: MaterialOutcome
    subject: str
    predicate: str
    scope: str
    object: str
    polarity: Polarity
    as_of_period: str
    derived: bool
    when: str  # "Change detected on <date>" for a derived period, never "Changed on"
    report_id: str  # the new Report
    evidence_id: str
    previous_object: str | None = None
    previous_polarity: Polarity | None = None
    previous_report_id: str | None = None  # the Report that established the earlier value
    previous_run_id: str | None = None


class Alert(BaseModel):
    id: str
    owner_id: str
    watchlist_item_id: str
    run_id: str
    report_id: str
    brief: str
    created_at: datetime
    changes: list[AlertChange]
    failed_diffs: list[FailedDiff] = Field(default_factory=list)
    read: bool = False


DeliveryStatus = Literal["pending", "delivered", "failed"]


class WebhookDelivery(BaseModel):
    """Delivery of one Alert to its owner's webhook, kept apart from the Alert itself.
    At least once: a crash between the POST and recording it means a second POST."""

    owner_id: str
    alert_id: str
    url: str
    status: DeliveryStatus = "pending"
    attempts: int = 0
    next_attempt_at: datetime
    last_error: str = ""
    delivered_at: datetime | None = None


def change_wording(finding: Finding) -> str:
    """A derived period says when the state was observed, never when it changed."""
    if finding.derived:
        return f"Change detected on {finding.as_of_period}"
    return f"As of {finding.as_of_period}"


def alert_change(entry: DiffEntry) -> AlertChange:
    new, old = entry.finding, entry.previous
    return AlertChange(
        outcome=entry.outcome,
        subject=new.subject,
        predicate=new.predicate,
        scope=new.scope,
        object=new.object,
        polarity=new.polarity,
        as_of_period=new.as_of_period,
        derived=new.derived,
        when=change_wording(new),
        report_id=new.provenance.report_id,
        evidence_id=new.provenance.evidence_id,
        previous_object=old.object if old else None,
        previous_polarity=old.polarity if old else None,
        previous_report_id=old.provenance.report_id if old else None,
        previous_run_id=old.provenance.run_id if old else None,
    )


def alert_id(run_id: str) -> str:
    """One Alert per Run, by construction as well as by a UNIQUE constraint."""
    return f"al_{run_id}"
