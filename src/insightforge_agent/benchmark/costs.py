"""Offline cost-control primitives: exact money, a durable hash-chained ledger, per-call
worst-case reservation, and a sequential runner. Used with scripted fakes only in M1.

Authorisation is a prerequisite this module cannot grant. A CLI `--approved-budget` number is
`Authorization.unverified(...)`, which `BudgetGuard` refuses. M2 code must construct a verified
`Authorization` only after checking the owner's approval (docs/benchmark/methodology.md)."""

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from insightforge_agent.benchmark.cases import PredictionCase
from insightforge_agent.benchmark.judge import Judge, JudgeStatus, Usage, Verdict, verdict_matches
from insightforge_agent.benchmark.recording import RecordingStore, RunIdentity, redact_text

_MILLION = Decimal(1_000_000)
_GENESIS = "0" * 64


class CostError(Exception):
    pass


class UnboundedCost(CostError):
    """A call has no provable maximum cost."""


class BudgetExceeded(CostError):
    pass


class AuthorizationError(CostError):
    pass


class LedgerError(CostError):
    """The ledger is corrupt or ambiguous. Nothing may run until the owner resolves it."""


class UnresolvedReservation(LedgerError):
    pass


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal

    def __post_init__(self) -> None:
        for value in (self.input_per_mtok, self.output_per_mtok):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("prices must be non-negative finite Decimals")


@dataclass(frozen=True)
class CallBound:
    """Provable ceilings for one call. `max_attempts` counts the first try plus retries."""

    max_input_tokens: int | None
    max_output_tokens: int | None
    max_attempts: int = 1


def worst_case_cost(pricing: Pricing, bound: CallBound | None) -> Decimal:
    if bound is None or bound.max_input_tokens is None or bound.max_output_tokens is None:
        raise UnboundedCost("call has no input and output token ceiling")
    if min(bound.max_input_tokens, bound.max_output_tokens) < 0 or bound.max_attempts < 1:
        raise UnboundedCost("call bound is invalid")
    per_attempt = (
        Decimal(bound.max_input_tokens) * pricing.input_per_mtok
        + Decimal(bound.max_output_tokens) * pricing.output_per_mtok
    ) / _MILLION
    return per_attempt * bound.max_attempts


def actual_cost(pricing: Pricing, usage: Usage) -> Decimal | None:
    """Exact cost of the reported usage, or None when it is not reliably known."""
    if not usage.tokens_known:
        return None
    return (
        Decimal(usage.input_tokens) * pricing.input_per_mtok
        + Decimal(usage.output_tokens) * pricing.output_per_mtok
    ) / _MILLION


@dataclass(frozen=True)
class Authorization:
    """Owner authorisation for a cumulative spend ceiling."""

    ceiling: Decimal
    reference: str
    verified: bool

    @classmethod
    def unverified(cls, ceiling: Decimal) -> "Authorization":
        """What a bare CLI budget number is. Never sufficient."""
        return cls(ceiling=ceiling, reference="cli --approved-budget (unverified)", verified=False)


def parse_money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a valid amount: {text!r}")
    return value


@dataclass
class _Reservation:
    amount: Decimal
    state: str = "open"  # open | settled | retained
    charged: Decimal = Decimal(0)


class Ledger:
    """Append-only JSONL, hash-chained, fsynced before use. Cumulative across runs and
    execution identifiers: nothing here resets when a new run id appears."""

    def __init__(self, path: Path, reservations: dict[str, _Reservation], head: str, seq: int):
        self._path, self._res, self._head, self._seq = path, reservations, head, seq

    @classmethod
    def open(cls, path: Path, *, recover_interrupted: bool = False) -> "Ledger":
        reservations: dict[str, _Reservation] = {}
        head, seq = _GENESIS, 0
        if path.exists():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    entry = json.loads(line)
                    event = entry["event"]
                    if entry["seq"] != seq + 1 or entry["prev"] != head:
                        raise LedgerError("sequence or hash chain broken")
                    if entry["hash"] != _digest(head, event):
                        raise LedgerError("entry hash mismatch")
                    _apply(reservations, event)
                except LedgerError as exc:
                    raise LedgerError(f"ledger {path.name} line {number}: {exc}") from exc
                except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                    raise LedgerError(f"ledger {path.name} line {number} is unreadable: {exc!r}") from exc
                head, seq = entry["hash"], entry["seq"]
        ledger = cls(path, reservations, head, seq)
        open_ids = [i for i, r in reservations.items() if r.state == "open"]
        if open_ids and not recover_interrupted:
            raise UnresolvedReservation(
                f"{len(open_ids)} reservation(s) were never settled (interrupted run?): {open_ids}"
            )
        for reservation_id in open_ids:
            ledger.retain(reservation_id, "recovered after interruption; worst case kept as spent")
        return ledger

    @property
    def committed(self) -> Decimal:
        """Spent plus conservatively retained plus currently reserved."""
        total = Decimal(0)
        for r in self._res.values():
            total += r.amount if r.state in ("open", "retained") else r.charged
        return total

    @property
    def open_reservations(self) -> int:
        return sum(1 for r in self._res.values() if r.state == "open")

    def reserve(self, reservation_id: str, call: dict[str, str], amount: Decimal) -> None:
        if self.open_reservations:
            raise CostError("execution is sequential: a reservation is already open")
        self._write({"type": "reserve", "id": reservation_id, "call": call, "amount": str(amount)})

    def settle(self, reservation_id: str, actual: Decimal) -> None:
        self._write({"type": "settle", "id": reservation_id, "actual": str(actual)})

    def retain(self, reservation_id: str, reason: str) -> None:
        self._write({"type": "retain", "id": reservation_id, "reason": redact_text(reason)})

    def next_id(self) -> str:
        return f"res-{self._seq + 1:06d}"

    def _write(self, event: dict) -> None:
        _apply(self._res, event)  # validates before anything is written
        entry = {"seq": self._seq + 1, "prev": self._head, "event": event,
                 "hash": _digest(self._head, event)}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._head, self._seq = entry["hash"], entry["seq"]


def _digest(prev: str, event: dict) -> str:
    body = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def _apply(reservations: dict[str, _Reservation], event: dict) -> None:
    kind, rid = event["type"], event["id"]
    if kind == "reserve":
        amount = Decimal(event["amount"])
        if rid in reservations or not amount.is_finite() or amount < 0:
            raise LedgerError(f"invalid or duplicate reservation {rid}")
        reservations[rid] = _Reservation(amount)
        return
    reservation = reservations.get(rid)
    if kind not in ("settle", "retain") or reservation is None or reservation.state != "open":
        raise LedgerError(f"{kind} of unknown or already closed reservation {rid}")
    if kind == "settle":
        actual = Decimal(event["actual"])
        if not actual.is_finite() or actual < 0:
            raise LedgerError(f"invalid actual amount for {rid}")
        reservation.state, reservation.charged = "settled", actual
    else:
        reservation.state, reservation.charged = "retained", reservation.amount


@dataclass(frozen=True)
class Settlement:
    charged: Decimal
    kind: str  # actual | retained_unknown_usage | retained_failure | actual_over_reservation


class BudgetGuard:
    def __init__(self, ledger: Ledger, authorization: Authorization, pricing: Pricing):
        if not authorization.verified or not authorization.reference.strip():
            raise AuthorizationError(
                "a budget number alone is not owner authorisation; M2 must verify the approval"
            )
        if not authorization.ceiling.is_finite() or authorization.ceiling < 0:
            raise AuthorizationError("authorised ceiling is invalid")
        self.ledger, self.authorization, self.pricing = ledger, authorization, pricing

    @property
    def remaining(self) -> Decimal:
        return self.authorization.ceiling - self.ledger.committed

    def preflight(self, bounds: Iterable[CallBound | None]) -> Decimal:
        """Conservative high estimate for the whole job; refuse before any call if it will not fit."""
        estimate = sum((worst_case_cost(self.pricing, b) for b in bounds), Decimal(0))
        if estimate > self.remaining:
            raise BudgetExceeded(f"high estimate {estimate} exceeds remaining budget {self.remaining}")
        return estimate

    def reserve(self, call: dict[str, str], bound: CallBound | None) -> tuple[str, Decimal]:
        worst = worst_case_cost(self.pricing, bound)
        if worst > self.remaining:
            raise BudgetExceeded(f"worst case {worst} exceeds remaining budget {self.remaining}")
        reservation_id = self.ledger.next_id()
        self.ledger.reserve(reservation_id, call, worst)
        return reservation_id, worst

    def settle(self, reservation_id: str, reserved: Decimal, usage: Usage | None) -> Settlement:
        actual = actual_cost(self.pricing, usage) if usage is not None else None
        if actual is None:
            self.ledger.retain(reservation_id, "usage unknown; worst case kept as spent")
            return Settlement(reserved, "retained_unknown_usage")
        self.ledger.settle(reservation_id, actual)
        return Settlement(actual, "actual" if actual <= reserved else "actual_over_reservation")

    def fail(self, reservation_id: str, reserved: Decimal, reason: str) -> Settlement:
        self.ledger.retain(reservation_id, reason)
        return Settlement(reserved, "retained_failure")


@dataclass
class RunSummary:
    completed: list[str] = field(default_factory=list)
    skipped_recorded: list[str] = field(default_factory=list)
    halted_reason: str | None = None
    charged: Decimal = Decimal(0)


def run_sequential(
    *,
    cases: Sequence[PredictionCase],
    judge: Judge,
    guard: BudgetGuard,
    bound_for: Callable[[PredictionCase], CallBound | None],
    store: RecordingStore,
    identity: RunIdentity,
) -> RunSummary:
    """Judge cases one at a time. Cases already recorded under this identity are skipped (this is
    how an interrupted run resumes without paying twice). A provider failure or exhausted budget
    halts the run with every earlier result kept; the failed call's worst case stays spent."""
    summary = RunSummary()
    pending = []
    for case in cases:
        if store.get(identity.candidate, case.id, identity.run) is not None:
            summary.skipped_recorded.append(case.id)
        else:
            pending.append(case)
    guard.preflight(bound_for(case) for case in pending)  # raises before any call is made

    for case in pending:
        call = {"candidate": identity.candidate, "case_id": case.id, "run": identity.run}
        try:
            reservation_id, reserved = guard.reserve(call, bound_for(case))
        except BudgetExceeded as exc:
            summary.halted_reason = str(exc)
            break
        try:
            verdict = judge.judge(case)
            if not verdict_matches(case, verdict):
                raise CostError("judge returned a verdict for a different case")
        except Exception as exc:  # a failed call: keep its worst case, record the failure, stop
            reason = f"{type(exc).__name__}: {redact_text(str(exc))[:200]}"
            summary.charged += guard.fail(reservation_id, reserved, reason).charged
            store.record(identity, case, Verdict(
                case_id=case.id, case_type=case.type, status=JudgeStatus.ERROR, reason=reason))
            summary.halted_reason = f"judge failed on {case.id}: {reason}"
            break
        summary.charged += guard.settle(reservation_id, reserved, verdict.usage).charged
        store.record(identity, case, verdict)
        summary.completed.append(case.id)
    return summary
