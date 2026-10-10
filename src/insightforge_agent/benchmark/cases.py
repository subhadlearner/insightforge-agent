"""Benchmark case records: typed, validated, JSONL on disk (docs/benchmark/methodology.md)."""

import json
import re
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CaseType(StrEnum):
    PAIR = "PAIR"
    SUPPORT = "SUPPORT"
    GROUND = "GROUND"
    INDEPENDENCE = "INDEPENDENCE"


class PairLabel(StrEnum):
    SAME_FACT = "SAME_FACT"
    DIFFERENT_FACT = "DIFFERENT_FACT"
    CONTRADICTORY = "CONTRADICTORY"


class SupportLabel(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class GroundLabel(StrEnum):
    GROUNDED = "GROUNDED"
    NOT_GROUNDED = "NOT_GROUNDED"


class IndependenceLabel(StrEnum):
    INDEPENDENT = "INDEPENDENT"
    DEPENDENT = "DEPENDENT"
    UNDETERMINED = "UNDETERMINED"


class GroundingFailure(StrEnum):
    """Why a Claim is NOT_GROUNDED. Diagnostic only; the judgment label stays binary."""

    UNSUPPORTED_ENTITY = "UNSUPPORTED_ENTITY"
    UNSUPPORTED_NUMBER = "UNSUPPORTED_NUMBER"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    MISSING_CITATION = "MISSING_CITATION"


Label = PairLabel | SupportLabel | GroundLabel | IndependenceLabel

LABELS_BY_TYPE: dict[CaseType, type[StrEnum]] = {
    CaseType.PAIR: PairLabel,
    CaseType.SUPPORT: SupportLabel,
    CaseType.GROUND: GroundLabel,
    CaseType.INDEPENDENCE: IndependenceLabel,
}

# The positive class is the one a false alarm is measured against: a false merge, false
# support, an ungrounded Claim passed as grounded, or false independence. Every other label of
# the type is a negative.
POSITIVE_LABEL: dict[CaseType, StrEnum] = {
    CaseType.PAIR: PairLabel.SAME_FACT,
    CaseType.SUPPORT: SupportLabel.SUPPORTED,
    CaseType.GROUND: GroundLabel.GROUNDED,
    CaseType.INDEPENDENCE: IndependenceLabel.INDEPENDENT,
}


def label_belongs_to(case_type: CaseType, label: object) -> bool:
    return isinstance(label, LABELS_BY_TYPE[case_type])


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALL_LABELS = [m.value for enum in LABELS_BY_TYPE.values() for m in enum]


class InputAccess(StrEnum):
    """Which inputs a candidate may receive. SEMANTIC candidates (a model reading text) get
    statements and Passages only; BASELINE (the deterministic T5 logic) also gets the
    structured fields its merge key is built from."""

    SEMANTIC = "SEMANTIC"
    BASELINE = "BASELINE"


class MergeFields(_Frozen):
    """The structured fields T5's drafting step produces for one candidate item. They feed the
    baseline's merge key (group key plus period plus canonical value). `period` is the stated
    as-of period text; empty means unstated."""

    entity: str = ""
    predicate: str = ""
    value: str = ""
    scope: str = ""
    period: str = ""
    entities: list[str] = Field(default_factory=list)


class PairInputs(_Frozen):
    statement_a: str
    statement_b: str
    passage_a: str | None = None
    passage_b: str | None = None
    # Baseline-only: withheld from SEMANTIC candidates. Both or neither.
    structured_a: MergeFields | None = None
    structured_b: MergeFields | None = None

    @model_validator(mode="after")
    def _structured_together(self) -> "PairInputs":
        if (self.structured_a is None) != (self.structured_b is None):
            raise ValueError("structured_a and structured_b must be given together")
        return self


class SupportInputs(_Frozen):
    statement: str
    passage: str


class GroundCheck(StrEnum):
    """What is being grounded. QUOTED_SPAN: an Evidence statement against the Passage span that
    is quoted for it. CLAIM_PASSAGE: a Report Claim against a Passage (T6 Fact-Checker, which
    quotes no span). They are separate checks and are reported separately."""

    QUOTED_SPAN = "QUOTED_SPAN"
    CLAIM_PASSAGE = "CLAIM_PASSAGE"


class PassageIdentity(_Frozen):
    observation_id: str
    index: int = Field(ge=0)


class GroundInputs(_Frozen):
    check: GroundCheck
    claim: str  # the statement under test: Evidence statement or Report Claim text
    passage_text: str  # the original, whole Passage text
    passage_ref: PassageIdentity
    quoted_span: str | None = None  # required for QUOTED_SPAN, absent for CLAIM_PASSAGE
    # Text that says whom or what the Passage is about (heading, speaker, table caption, the
    # neighbouring sentence): what attribution of entity, measure and period depends on.
    attribution_context: str | None = None
    historical: bool = False  # the Claim's own flag (T6); meaningful for CLAIM_PASSAGE

    @model_validator(mode="after")
    def _span_matches_check(self) -> "GroundInputs":
        has_span = bool((self.quoted_span or "").strip())
        if self.check is GroundCheck.QUOTED_SPAN and not has_span:
            raise ValueError("a QUOTED_SPAN case needs quoted_span")
        if self.check is GroundCheck.CLAIM_PASSAGE and self.quoted_span is not None:
            raise ValueError("a CLAIM_PASSAGE case quotes no span")
        return self


class IndependenceInputs(_Frozen):
    source_a_url: str
    source_a_text: str
    source_b_url: str
    source_b_text: str


Inputs = PairInputs | SupportInputs | GroundInputs | IndependenceInputs

INPUTS_BY_TYPE: dict[CaseType, type[_Frozen]] = {
    CaseType.PAIR: PairInputs,
    CaseType.SUPPORT: SupportInputs,
    CaseType.GROUND: GroundInputs,
    CaseType.INDEPENDENCE: IndependenceInputs,
}


class LabelStatus(StrEnum):
    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    DISPUTED = "disputed"


def _require_text(value: str, what: str) -> None:
    if not value.strip():
        raise ValueError(f"{what} must not be empty")


class BenchmarkCase(_Frozen):
    id: str
    type: CaseType
    primary_category: str
    tags: list[str] = Field(default_factory=list)
    origin: Literal["synthetic", "excerpt"]
    source_url: str | None = None
    inputs: Inputs
    expected: Label
    grounding_failure: GroundingFailure | None = None
    rationale: str
    critical: bool = False
    label_status: LabelStatus = LabelStatus.PROPOSED
    reviewer: str | None = None
    reviewed_at: date | None = None
    dispute_note: str | None = None
    second_reviewer: str | None = None
    second_reviewed_at: date | None = None
    second_label: Label | None = None  # the second reviewer's own judgment, kept even if it differs

    @model_validator(mode="before")
    @classmethod
    def _typed_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("inputs"), dict):
            try:
                inputs_model = INPUTS_BY_TYPE[CaseType(data.get("type"))]
            except ValueError:
                return data  # the type field's own error is reported
            data = {**data, "inputs": inputs_model(**data["inputs"])}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> "BenchmarkCase":
        if not _ID.match(self.id):
            raise ValueError("id must be alphanumeric with . _ - separators")
        folded = self.id.lower().replace("-", "_")
        if any(label.lower() in folded for label in _ALL_LABELS):
            raise ValueError("id must not contain a label name (it would leak the answer)")
        _require_text(self.primary_category, "primary_category")
        _require_text(self.rationale, "rationale")
        if any(not tag.strip() for tag in self.tags) or len(set(self.tags)) != len(self.tags):
            raise ValueError("tags must be nonempty and unique")
        if not isinstance(self.inputs, INPUTS_BY_TYPE[self.type]):
            raise ValueError(f"inputs do not match case type {self.type}")
        for name, value in self.inputs:
            optional_passage = name in ("passage_a", "passage_b")
            if isinstance(value, str) and not value.strip() and not optional_passage:
                raise ValueError(f"inputs.{name} must not be empty")
        if not label_belongs_to(self.type, self.expected):
            raise ValueError(f"expected {self.expected} is not a {self.type} label")
        if self.origin == "excerpt" and not (self.source_url or "").startswith(("http://", "https://")):
            raise ValueError("excerpt cases need an http(s) source_url")
        if self.grounding_failure is not None and self.expected != GroundLabel.NOT_GROUNDED:
            raise ValueError("grounding_failure applies only to expected NOT_GROUNDED")
        self._check_review()
        return self

    def _check_review(self) -> None:
        second = (self.second_reviewer, self.second_reviewed_at, self.second_label)
        has_second = any(v is not None for v in second)
        if has_second and any(v is None for v in second):
            raise ValueError("second review needs reviewer, date and label together")
        if self.second_label is not None and not label_belongs_to(self.type, self.second_label):
            raise ValueError(f"second_label {self.second_label} is not a {self.type} label")
        if self.label_status is LabelStatus.PROPOSED:
            if self.reviewer or self.reviewed_at or self.dispute_note or has_second:
                raise ValueError("a proposed label carries no review or dispute metadata")
            return
        if self.label_status is LabelStatus.REVIEWED:
            if not (self.reviewer or "").strip() or self.reviewed_at is None:
                raise ValueError("a reviewed label needs reviewer and reviewed_at")
            if self.dispute_note:
                raise ValueError("a reviewed label cannot carry a dispute note")
            if has_second and self.second_label != self.expected:
                raise ValueError("a second review that differs from expected makes the label disputed")
            if has_second and self.second_reviewer == self.reviewer:
                raise ValueError("second reviewer must differ from the first")
            return
        if not (self.dispute_note or "").strip():
            raise ValueError("a disputed label needs a dispute_note")

    @property
    def second_review_agrees(self) -> bool | None:
        return None if self.second_label is None else self.second_label == self.expected

    def prediction_view(self, access: InputAccess = InputAccess.SEMANTIC) -> "PredictionCase":
        inputs = self.inputs
        if isinstance(inputs, PairInputs) and access is InputAccess.SEMANTIC:
            inputs = inputs.model_copy(update={"structured_a": None, "structured_b": None})
        return PredictionCase(id=self.id, type=self.type, inputs=inputs, access=access)


class PredictionCase(_Frozen):
    """All a Judge may see. No expected label, rationale, category, review data or origin."""

    id: str
    type: CaseType
    inputs: Inputs
    access: InputAccess = InputAccess.SEMANTIC

    @model_validator(mode="after")
    def _semantic_sees_no_baseline_fields(self) -> "PredictionCase":
        if (self.access is InputAccess.SEMANTIC and isinstance(self.inputs, PairInputs)
                and self.inputs.structured_a is not None):
            raise ValueError("SEMANTIC candidates must not receive baseline structured fields")
        return self


class CaseFileError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def load_cases(path: Path) -> list[BenchmarkCase]:
    """Read and validate a JSONL case file, reporting every problem with its line number."""
    cases: list[BenchmarkCase] = []
    problems: list[str] = []
    first_seen: dict[str, int] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("a record must be a JSON object")
            case = BenchmarkCase.model_validate(raw)
        except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
            problems.append(f"line {number}: {_short(exc)}")
            continue
        if case.id in first_seen:
            problems.append(
                f"line {number}: duplicate id {case.id!r} (first on line {first_seen[case.id]})"
            )
            continue
        first_seen[case.id] = number
        cases.append(case)
    if problems:
        raise CaseFileError(problems)
    return cases


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'record'}: {e['msg']}" for e in exc.errors()
        )
    return str(exc)
