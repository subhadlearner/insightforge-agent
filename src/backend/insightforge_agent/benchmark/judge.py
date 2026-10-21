"""Judge and Verdict contracts. Model-agnostic: nothing here names a provider or model family.

A Judge sees only a `PredictionCase`. A Verdict never contains the expected label, and model
scores are diagnostics only: they never set Evidence Confidence (ADR-0005 rule 4)."""

import math
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from insightforge_agent.benchmark.cases import (
    CaseType,
    GroundingFailure,
    GroundLabel,
    Label,
    PredictionCase,
    label_belongs_to,
)

Scalar = str | int | float | bool

# Diagnostic keys a report aggregates. A candidate that can say what its own Evidence pipeline
# would do to the item under test records it here; others leave them out (never zero-filled).
DISPOSITION_KEY = "evidence_disposition"
REASON_KEY = "evidence_reason"


class JudgeStatus(StrEnum):
    JUDGED = "JUDGED"
    ABSTAIN = "ABSTAIN"
    ERROR = "ERROR"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Usage(BaseModel):
    """What a call cost. None means unknown, never zero: a judge that made no call must say so
    with explicit zeros. Token counts are totals across all attempts, retries included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    parse_failures: int | None = Field(default=None, ge=0)

    @field_validator("latency_ms")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("latency_ms must be finite")
        return value

    @property
    def tokens_known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None


class Verdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    case_type: CaseType
    status: JudgeStatus
    label: Label | None = None
    reason: str | None = None
    grounding_failure: GroundingFailure | None = None
    usage: Usage = Field(default_factory=Usage)
    model_scores: dict[str, float] | None = None
    diagnostics: dict[str, Scalar] = Field(default_factory=dict)

    @field_validator("model_scores")
    @classmethod
    def _finite_scores(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is not None and not all(math.isfinite(v) for v in value.values()):
            raise ValueError("model scores must be finite")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "Verdict":
        if self.status is JudgeStatus.JUDGED:
            if self.label is None:
                raise ValueError("a JUDGED verdict needs a label")
            if not label_belongs_to(self.case_type, self.label):
                raise ValueError(f"{self.label} is not a {self.case_type} label")
        else:
            if self.label is not None:
                raise ValueError(f"a {self.status} verdict carries no label")
            if not (self.reason or "").strip():
                raise ValueError(f"a {self.status} verdict needs a reason")
        if self.grounding_failure is not None and self.label != GroundLabel.NOT_GROUNDED:
            raise ValueError("grounding_failure applies only to a NOT_GROUNDED verdict")
        return self


class Judge(Protocol):
    def judge(self, case: PredictionCase) -> Verdict: ...


def verdict_matches(case: PredictionCase, verdict: Verdict) -> bool:
    return verdict.case_id == case.id and verdict.case_type == case.type
