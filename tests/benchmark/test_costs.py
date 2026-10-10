from decimal import Decimal

import pytest

from insightforge_agent.benchmark.costs import (
    Authorization,
    AuthorizationError,
    BudgetExceeded,
    BudgetGuard,
    CallBound,
    CostError,
    Ledger,
    LedgerError,
    Pricing,
    UnboundedCost,
    UnresolvedReservation,
    actual_cost,
    parse_money,
    run_sequential,
    worst_case_cost,
)
from insightforge_agent.benchmark.judge import Usage, Verdict
from insightforge_agent.benchmark.recording import RecordingStore, RunIdentity

from .helpers import make_case

# $1 per million input tokens, $2 per million output tokens: easy exact arithmetic.
PRICING = Pricing(Decimal("1"), Decimal("2"))
BOUND = CallBound(max_input_tokens=1000, max_output_tokens=500)  # worst case 0.002 per attempt
WORST = Decimal("0.002")


def auth(ceiling="1", verified=True):
    return Authorization(Decimal(ceiling), "owner approval #1", verified)


def make_guard(tmp_path, ceiling="1", **kw):
    return BudgetGuard(Ledger.open(tmp_path / "ledger.jsonl", **kw), auth(ceiling), PRICING)


def cases(n):
    return [make_case(case_id=f"k{i}").prediction_view() for i in range(n)]


def identity(run="r1"):
    return RunIdentity(candidate="fake", candidate_version="1", model="m", run=run)


class ScriptedJudge:
    """Returns scripted usage per call (or raises), and records the ledger state it saw."""

    def __init__(self, script, guard=None):
        self.script, self.guard, self.calls, self.committed_during = list(script), guard, 0, []

    def judge(self, case):
        self.calls += 1
        if self.guard is not None:
            self.committed_during.append(self.guard.ledger.committed)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Verdict(case_id=case.id, case_type=case.type, status="JUDGED", label="SAME_FACT",
                       usage=step)


def run(tmp_path, judge_script, n, ceiling="1", run_id="r1", bound=BOUND):
    guard = make_guard(tmp_path, ceiling)
    store = RecordingStore(tmp_path / "rec")
    judge = ScriptedJudge(judge_script, guard)
    summary = run_sequential(cases=cases(n), judge=judge, guard=guard, bound_for=lambda c: bound,
                             store=store, identity=identity(run_id))
    return summary, guard, store, judge


# ---- arithmetic -------------------------------------------------------------------------------

def test_money_is_exact_decimal():
    assert worst_case_cost(PRICING, BOUND) == WORST
    assert worst_case_cost(PRICING, CallBound(1000, 500, max_attempts=3)) == 3 * WORST
    assert actual_cost(PRICING, Usage(input_tokens=1, output_tokens=1)) == Decimal("0.000003")
    third = Pricing(Decimal("0.1"), Decimal("0.2"))
    assert sum([actual_cost(third, Usage(input_tokens=1, output_tokens=0))] * 10) == Decimal("0.000001")


def test_usage_that_is_not_fully_known_has_no_actual_cost():
    assert actual_cost(PRICING, Usage()) is None
    assert actual_cost(PRICING, Usage(input_tokens=5)) is None


@pytest.mark.parametrize("bound", [None, CallBound(None, 10), CallBound(10, None), CallBound(10, 10, 0)])
def test_a_call_without_a_provable_ceiling_is_unbounded(bound):
    with pytest.raises(UnboundedCost):
        worst_case_cost(PRICING, bound)


def test_amounts_parse_strictly():
    assert parse_money("0.50") == Decimal("0.50")
    for bad in ("abc", "-1", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            parse_money(bad)


# ---- authorisation ----------------------------------------------------------------------------

def test_a_cli_budget_number_alone_is_not_authorisation(tmp_path):
    ledger = Ledger.open(tmp_path / "l.jsonl")
    with pytest.raises(AuthorizationError):
        BudgetGuard(ledger, Authorization.unverified(Decimal("5")), PRICING)
    with pytest.raises(AuthorizationError):
        BudgetGuard(ledger, Authorization(Decimal("5"), "  ", True), PRICING)


# ---- preflight and reservations ---------------------------------------------------------------

def test_preflight_refuses_before_any_call_when_the_high_estimate_exceeds_the_budget(tmp_path):
    guard = make_guard(tmp_path, ceiling="0.005")  # fits two calls at 0.002, not three
    store = RecordingStore(tmp_path / "rec")
    judge = ScriptedJudge([Usage(input_tokens=1, output_tokens=1)] * 3, guard)
    with pytest.raises(BudgetExceeded):
        run_sequential(cases=cases(3), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                       store=store, identity=identity())
    assert judge.calls == 0 and guard.ledger.committed == 0
    assert store.records_for("fake", "r1") == []


def test_an_unbounded_case_refuses_the_whole_run_before_any_call(tmp_path):
    guard = make_guard(tmp_path)
    judge = ScriptedJudge([Usage(input_tokens=1, output_tokens=1)] * 2, guard)
    bounds = iter([BOUND, None])
    with pytest.raises(UnboundedCost):
        run_sequential(cases=cases(2), judge=judge, guard=guard, bound_for=lambda c: next(bounds),
                       store=RecordingStore(tmp_path / "rec"), identity=identity())
    assert judge.calls == 0


def test_the_worst_case_is_reserved_before_each_call_then_reconciled_to_actual(tmp_path):
    usage = Usage(input_tokens=100, output_tokens=50)  # actual 0.0002
    summary, guard, _, judge = run(tmp_path, [usage, usage], 2)
    assert judge.committed_during == [WORST, Decimal("0.0002") + WORST]
    assert guard.ledger.committed == Decimal("0.0004")
    assert summary.charged == Decimal("0.0004") and summary.halted_reason is None


def test_retries_are_charged_from_total_reported_usage_and_bounded_by_attempts(tmp_path):
    bound = CallBound(1000, 500, max_attempts=3)
    retried = Usage(input_tokens=2000, output_tokens=900, retries=2)  # two attempts' worth
    summary, guard, _, judge = run(tmp_path, [retried], 1, bound=bound)
    assert judge.committed_during == [3 * WORST]
    assert summary.charged == Decimal("0.0038")  # 2000*1 + 900*2 per million


def test_actual_usage_above_the_reservation_is_charged_in_full(tmp_path):
    over = Usage(input_tokens=5000, output_tokens=5000)  # 0.015 > 0.002
    summary, guard, _, _ = run(tmp_path, [over], 1)
    assert guard.ledger.committed == Decimal("0.015") and summary.charged == Decimal("0.015")


def test_unknown_usage_keeps_the_worst_case_as_spent(tmp_path):
    summary, guard, _, _ = run(tmp_path, [Usage()], 1)
    assert guard.ledger.committed == WORST and summary.charged == WORST


def test_execution_is_sequential_one_open_reservation_at_a_time(tmp_path):
    guard = make_guard(tmp_path)
    guard.reserve({"candidate": "c", "case_id": "a", "run": "r"}, BOUND)
    with pytest.raises(CostError):
        guard.reserve({"candidate": "c", "case_id": "b", "run": "r"}, BOUND)


# ---- failure and partial results --------------------------------------------------------------

def test_a_provider_failure_keeps_earlier_results_and_the_failed_calls_worst_case(tmp_path):
    ok = Usage(input_tokens=100, output_tokens=50)
    summary, guard, store, judge = run(
        tmp_path, [ok, RuntimeError("503 from provider key=sk-ABCDEFGHIJKLMNOPQRS"), ok], 3)
    assert summary.completed == ["k0"] and judge.calls == 2
    assert summary.halted_reason.startswith("judge failed on k1")
    assert "ABCDEFGHIJKLMNOPQRS" not in summary.halted_reason
    assert guard.ledger.committed == Decimal("0.0002") + WORST
    assert store.get("fake", "k0", "r1").verdict.status.value == "JUDGED"
    failed = store.get("fake", "k1", "r1").verdict
    assert failed.status.value == "ERROR" and failed.usage.input_tokens is None
    assert store.get("fake", "k2", "r1") is None


def test_a_verdict_for_the_wrong_case_is_a_failure(tmp_path):
    guard = make_guard(tmp_path)
    other = make_case(case_id="zzz").prediction_view()

    class Wrong:
        def judge(self, case):
            return Verdict(case_id=other.id, case_type=other.type, status="JUDGED", label="SAME_FACT",
                           usage=Usage(input_tokens=1, output_tokens=1))

    summary = run_sequential(cases=cases(1), judge=Wrong(), guard=guard, bound_for=lambda c: BOUND,
                             store=RecordingStore(tmp_path / "rec"), identity=identity())
    assert "different case" in summary.halted_reason and guard.ledger.committed == WORST


def test_preflight_counts_charges_retained_by_earlier_executions(tmp_path):
    guard = make_guard(tmp_path, ceiling="0.004")
    pre = guard.ledger  # leave only 0.003 remaining after a prior retained charge
    rid, _ = guard.reserve({"candidate": "x", "case_id": "x", "run": "x"}, CallBound(500, 250))
    pre.retain(rid, "earlier unknown")  # 0.001 retained
    judge = ScriptedJudge([Usage(input_tokens=1, output_tokens=1)] * 2, guard)
    store = RecordingStore(tmp_path / "rec")
    # preflight for 2 calls = 0.004 > remaining 0.003 -> refused outright
    with pytest.raises(BudgetExceeded):
        run_sequential(cases=cases(2), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                       store=store, identity=identity())
    assert judge.calls == 0


def test_actual_overrun_mid_run_halts_the_remaining_cases(tmp_path):
    over = Usage(input_tokens=1000, output_tokens=1000)  # actual 0.003 vs reserved 0.002
    summary, guard, store, judge = run(tmp_path, [over, over], 2, ceiling="0.0045")
    assert summary.completed == ["k0"] and "exceeded the reserved worst case" in summary.halted_reason
    assert judge.calls == 1 and store.get("fake", "k1", "r1") is None


# ---- durability, restart, corruption ----------------------------------------------------------

def test_cumulative_spend_persists_across_restarts_and_new_run_ids(tmp_path):
    ok = Usage(input_tokens=100, output_tokens=50)
    run(tmp_path, [ok], 1, run_id="r1")
    reopened = Ledger.open(tmp_path / "ledger.jsonl")
    assert reopened.committed == Decimal("0.0002")
    summary, guard, _, _ = run(tmp_path, [ok], 1, run_id="a-brand-new-run")
    assert guard.ledger.committed == Decimal("0.0004")  # a new run id restores nothing


def test_a_new_execution_cannot_exceed_the_cumulative_ceiling(tmp_path):
    run(tmp_path, [Usage()], 1, ceiling="0.003")  # retains 0.002, leaving 0.001
    with pytest.raises(BudgetExceeded):
        run(tmp_path, [Usage()], 1, ceiling="0.003", run_id="again")


def test_resuming_skips_recorded_cases_and_does_not_pay_twice(tmp_path):
    ok = Usage(input_tokens=100, output_tokens=50)
    run(tmp_path, [ok, RuntimeError("boom")], 3)
    guard = make_guard(tmp_path)
    store = RecordingStore(tmp_path / "rec")
    judge = ScriptedJudge([ok, ok], guard)
    # k0 recorded; k1 recorded as ERROR (not retried implicitly); only k2 is new
    summary = run_sequential(cases=cases(3), judge=judge, guard=guard, bound_for=lambda c: BOUND,
                             store=store, identity=identity())
    assert summary.skipped_recorded == ["k0", "k1"] and summary.completed == ["k2"]
    assert judge.calls == 1


def test_an_interrupted_reservation_fails_closed_until_explicitly_recovered(tmp_path):
    guard = make_guard(tmp_path)
    guard.reserve({"candidate": "c", "case_id": "a", "run": "r"}, BOUND)  # process dies here
    with pytest.raises(UnresolvedReservation):
        Ledger.open(tmp_path / "ledger.jsonl")
    recovered = Ledger.open(tmp_path / "ledger.jsonl", recover_interrupted=True)
    assert recovered.committed == WORST and recovered.open_reservations == 0
    assert Ledger.open(tmp_path / "ledger.jsonl").committed == WORST  # now clean


@pytest.mark.parametrize("damage", ["garbage", "truncate", "tamper", "drop_first", "bad_amount"])
def test_a_corrupt_ledger_fails_closed(tmp_path, damage):
    ok = Usage(input_tokens=100, output_tokens=50)
    run(tmp_path, [ok, ok], 2)
    path = tmp_path / "ledger.jsonl"
    lines = path.read_text().splitlines()
    if damage == "garbage":
        lines.append("{not json")
    elif damage == "truncate":
        lines[-1] = lines[-1][:20]
    elif damage == "tamper":
        lines[1] = lines[1].replace('"actual": "0.0002"', '"actual": "0.0000"')
    elif damage == "drop_first":
        lines = lines[1:]
    else:
        lines[0] = lines[0].replace('"amount": "0.002"', '"amount": "-1"')
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerError):
        Ledger.open(path)
