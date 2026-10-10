"""`insightforge-agent bench`: offline validation, reporting and the deterministic baseline.
There is deliberately no command that runs a model; the baseline runs only the production
deterministic logic. Paid or local inference is M2 and needs separate owner authorisation."""

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from insightforge_agent.benchmark.baseline import CANDIDATE as BASELINE_CANDIDATE
from insightforge_agent.benchmark.baseline import run_baseline
from insightforge_agent.benchmark.cases import CaseFileError, InputAccess, load_cases
from insightforge_agent.benchmark.costs import CostError, Ledger, parse_money
from insightforge_agent.benchmark.recording import RecordingError, RecordingStore
from insightforge_agent.benchmark.report import build_report, render_text, to_dict

DEFAULT_CASES = Path("benchmarks/evidence/cases.jsonl")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insightforge-agent bench",
        description="Offline benchmark tools. No model is ever called.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a JSONL case file")
    validate.add_argument("--cases", type=Path, default=DEFAULT_CASES)

    report = sub.add_parser("report", help="report recorded predictions against reviewed labels")
    report.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    report.add_argument("--recordings", type=Path, required=True)
    report.add_argument("--candidate", required=True)
    report.add_argument("--run", required=True)
    report.add_argument("--candidate-version",
                        help="use only recordings of this version (versions are never mixed)")
    report.add_argument("--input-access", type=InputAccess, choices=list(InputAccess),
                        default=InputAccess.SEMANTIC,
                        help="the input view the predictions were made on; a recording made on "
                             "the other view is stale (default SEMANTIC)")
    report.add_argument("--allow-proposed", action="store_true",
                        help="include proposed labels; the report is stamped EXPLORATORY")
    report.add_argument("--json", action="store_true")

    base = sub.add_parser(
        "baseline", help="run the real deterministic T5/T6 baseline (no model) and report it")
    base.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    base.add_argument("--recordings", type=Path, required=True)
    base.add_argument("--run", required=True)
    base.add_argument("--allow-proposed", action="store_true",
                      help="include proposed labels; the report is stamped EXPLORATORY")
    base.add_argument("--json", action="store_true")

    ledger = sub.add_parser("ledger", help="show cumulative spend from a cost ledger")
    ledger.add_argument("--ledger", type=Path, required=True)
    ledger.add_argument("--approved-budget", type=parse_money,
                        help="a number only; this is NOT owner authorisation")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ledger":
            return _ledger(args)
        if not args.cases.exists():
            print(f"error: case file not found: {args.cases}")
            return 2
        cases = load_cases(args.cases)
        if args.command == "validate":
            return _validate(args, cases)
        if args.command == "baseline":
            return _baseline(args, cases)
        return _report(args, cases)
    except CaseFileError as exc:
        print(f"{args.cases}: {len(exc.problems)} problem(s)")
        for problem in exc.problems:
            print(f"  {problem}")
        return 1
    except (CostError, RecordingError) as exc:
        print(f"error: {exc}")
        return 1


def _validate(args, cases) -> int:
    by_type = Counter(c.type.value for c in cases)
    by_status = Counter(c.label_status.value for c in cases)
    print(f"{args.cases}: {len(cases)} valid case(s)")
    print("  by type:   " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print("  by status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print(f"  critical:  {sum(c.critical for c in cases)}")
    return 0


def _report(args, cases) -> int:
    store = RecordingStore(args.recordings)
    report = build_report(cases, store, args.candidate, args.run, allow_proposed=args.allow_proposed,
                          candidate_version=args.candidate_version, access=args.input_access)
    _print(report, args.json)
    return 0


def _baseline(args, cases) -> int:
    store = RecordingStore(args.recordings)
    run_baseline(cases, store, args.run)
    report = build_report(cases, store, BASELINE_CANDIDATE, args.run,
                          allow_proposed=args.allow_proposed, access=InputAccess.BASELINE)
    _print(report, args.json)
    return 0


def _print(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(to_dict(report), indent=2, default=str))
    else:
        print(render_text(report))


def _ledger(args) -> int:
    ledger = Ledger.open(args.ledger)
    print(f"ledger {args.ledger}: committed (spent + retained liability) = {ledger.committed} USD")
    if args.approved_budget is not None:
        remaining = args.approved_budget - ledger.committed
        print(f"--approved-budget {args.approved_budget}: would leave {remaining} USD, "
              "but this number is necessary, not sufficient. UNVERIFIED: no owner authorisation "
              "has been checked, and no run can start from it.")
        if remaining < Decimal(0):
            print("  the committed amount already exceeds this number")
    return 0
