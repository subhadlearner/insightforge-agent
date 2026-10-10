"""Reports: counts, numerators, denominators and intervals per case type, category and expected
judgment. Predictions are joined to reviewed ground truth at read time; nothing here writes
either side. Denominator rules are documented in docs/benchmark/methodology.md."""

from collections import Counter
from dataclasses import asdict, dataclass, field

from insightforge_agent.benchmark.cases import POSITIVE_LABEL, BenchmarkCase, CaseType, LabelStatus
from insightforge_agent.benchmark.judge import JudgeStatus
from insightforge_agent.benchmark.recording import RecordingStore
from insightforge_agent.benchmark.stats import (
    Interval,
    Outcome,
    clopper_pearson_upper,
    f1_score,
    paired_bootstrap_f1,
    wilson_interval,
)

NOT_RECORDED = "NOT_RECORDED"
NO_SAFETY_PROOF = "Zero observed failures is not proof of safety; it only bounds the failure rate."


@dataclass(frozen=True)
class Proportion:
    numerator: int
    denominator: int
    value: float | None  # None when the denominator is 0: undefined, not zero
    interval: Interval | None


def proportion(numerator: int, denominator: int) -> Proportion:
    value = None if denominator == 0 else numerator / denominator
    return Proportion(numerator, denominator, value, wilson_interval(numerator, denominator))


@dataclass(frozen=True)
class GroupStats:
    eligible: int
    judged: int
    abstain: int
    error: int
    not_applicable: int
    not_recorded: int
    coverage: Proportion        # judged / eligible
    accuracy: Proportion        # correct / eligible; a missing prediction is wrong
    precision: Proportion       # TP / predicted positives (judged only)
    recall: Proportion          # TP / expected positives; a missing prediction is a miss
    false_positive_rate: Proportion   # FP / judged expected negatives
    worst_case_fpr: Proportion        # (FP + missing on negatives) / expected negatives
    f1: float | None
    false_positives: int
    judged_negatives: int
    fp_upper_bound: float | None      # one-sided 95% Clopper-Pearson on FP / judged negatives
    note: str | None = None


@dataclass(frozen=True)
class Exclusions:
    disputed: int
    proposed_not_allowed: int
    proposed_critical_not_allowed: int
    proposed_included: int


@dataclass(frozen=True)
class Report:
    mode: str  # QUALIFICATION | EXPLORATORY
    candidate: str
    candidate_version: str | None
    run: str
    exclusions: Exclusions
    overall: dict[str, GroupStats] = field(default_factory=dict)
    by_category: dict[str, GroupStats] = field(default_factory=dict)
    by_expected: dict[str, GroupStats] = field(default_factory=dict)
    f1_interval: dict[str, Interval | None] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Row:
    case: BenchmarkCase
    status: str
    predicted: object | None  # the label if JUDGED


def is_eligible(case: BenchmarkCase, allow_proposed: bool) -> bool:
    if case.label_status is LabelStatus.REVIEWED:
        return True
    return case.label_status is LabelStatus.PROPOSED and allow_proposed


def group_stats(rows: list[_Row]) -> GroupStats:
    counts = Counter(row.status for row in rows)
    eligible = len(rows)
    tp = fp = expected_pos = predicted_pos = judged_neg = missing_neg = correct = 0
    for row in rows:
        positive = row.case.expected == POSITIVE_LABEL[row.case.type]
        judged = row.status == JudgeStatus.JUDGED
        is_pos_pred = judged and row.predicted == POSITIVE_LABEL[row.case.type]
        correct += judged and row.predicted == row.case.expected
        expected_pos += positive
        predicted_pos += is_pos_pred
        tp += positive and is_pos_pred
        if not positive:
            fp += is_pos_pred
            judged_neg += judged
            missing_neg += not judged
    expected_neg = eligible - expected_pos
    fp_upper = clopper_pearson_upper(fp, judged_neg)
    note = NO_SAFETY_PROOF if fp == 0 and judged_neg > 0 else None
    outcomes = [Outcome(r.case.expected == POSITIVE_LABEL[r.case.type],
                        None if r.status != JudgeStatus.JUDGED
                        else r.predicted == POSITIVE_LABEL[r.case.type]) for r in rows]
    return GroupStats(
        eligible=eligible, judged=counts[JudgeStatus.JUDGED], abstain=counts[JudgeStatus.ABSTAIN],
        error=counts[JudgeStatus.ERROR], not_applicable=counts[JudgeStatus.NOT_APPLICABLE],
        not_recorded=counts[NOT_RECORDED],
        coverage=proportion(counts[JudgeStatus.JUDGED], eligible),
        accuracy=proportion(correct, eligible),
        precision=proportion(tp, predicted_pos),
        recall=proportion(tp, expected_pos),
        false_positive_rate=proportion(fp, judged_neg),
        worst_case_fpr=proportion(fp + missing_neg, expected_neg),
        f1=f1_score(outcomes), false_positives=fp, judged_negatives=judged_neg,
        fp_upper_bound=fp_upper, note=note,
    )


def build_report(
    cases: list[BenchmarkCase], store: RecordingStore, candidate: str, run: str,
    *, allow_proposed: bool = False,
) -> Report:
    records = {r.case_id: r for r in store.records_for(candidate, run)}
    rows: list[_Row] = []
    disputed = proposed_out = critical_out = proposed_in = 0
    for case in cases:
        if case.label_status is LabelStatus.DISPUTED:
            disputed += 1
            continue
        if not is_eligible(case, allow_proposed):
            proposed_out += 1
            critical_out += case.critical
            continue
        proposed_in += case.label_status is LabelStatus.PROPOSED
        record = records.get(case.id)
        if record is None:
            rows.append(_Row(case, NOT_RECORDED, None))
        else:
            verdict = record.verdict
            rows.append(_Row(case, verdict.status.value, verdict.label))

    exploratory = allow_proposed
    notices = []
    if exploratory:
        notices.append("EXPLORATORY: proposed labels are included. This is not a qualification "
                       "result and must not be cited as one.")
    else:
        notices.append("Qualification metrics use reviewed labels only. Disputed and proposed "
                       "cases are excluded and counted below.")
        if critical_out:
            notices.append(f"{critical_out} critical case(s) are excluded because their labels "
                           "are only proposed; pass --allow-proposed for an exploratory view.")
    notices.append(NO_SAFETY_PROOF)

    report = Report(
        mode="EXPLORATORY" if exploratory else "QUALIFICATION", candidate=candidate,
        candidate_version=next(iter(records.values())).candidate_version if records else None,
        run=run, exclusions=Exclusions(disputed, proposed_out, critical_out, proposed_in),
        notices=notices,
    )
    for case_type in CaseType:
        of_type = [r for r in rows if r.case.type == case_type]
        if not of_type:
            continue
        report.overall[case_type.value] = group_stats(of_type)
        report.f1_interval[case_type.value] = _f1_interval(of_type)
        for category in sorted({r.case.primary_category for r in of_type}):
            report.by_category[f"{case_type.value}/{category}"] = group_stats(
                [r for r in of_type if r.case.primary_category == category])
        for expected in sorted({r.case.expected.value for r in of_type}):
            report.by_expected[f"{case_type.value}/{expected}"] = group_stats(
                [r for r in of_type if r.case.expected.value == expected])
    return report


def _f1_interval(rows: list[_Row]) -> Interval | None:
    aligned = {
        r.case.id: Outcome(r.case.expected == POSITIVE_LABEL[r.case.type],
                           None if r.status != JudgeStatus.JUDGED
                           else r.predicted == POSITIVE_LABEL[r.case.type])
        for r in rows
    }
    return paired_bootstrap_f1({"candidate": aligned}).candidates["candidate"].interval


def _fmt(p: Proportion) -> str:
    if p.denominator == 0:
        return "undefined (0/0)"
    low, high = p.interval.low, p.interval.high
    return f"{p.numerator}/{p.denominator} = {p.value:.3f} [{low:.3f}, {high:.3f}]"


def _render_group(name: str, g: GroupStats, interval: Interval | None = None) -> list[str]:
    f1 = "undefined" if g.f1 is None else f"{g.f1:.3f}"
    if interval is not None and g.f1 is not None:
        f1 += f" [{interval.low:.3f}, {interval.high:.3f}] (paired bootstrap)"
    lines = [
        f"  {name}: eligible={g.eligible} judged={g.judged} abstain={g.abstain} error={g.error} "
        f"not_applicable={g.not_applicable} not_recorded={g.not_recorded}",
        f"    coverage        {_fmt(g.coverage)}",
        f"    accuracy        {_fmt(g.accuracy)}  (missing predictions count as wrong)",
        f"    precision       {_fmt(g.precision)}",
        f"    recall          {_fmt(g.recall)}  (missing predictions count as misses)",
        f"    false-pos rate  {_fmt(g.false_positive_rate)}  (judged negatives only)",
        f"    worst-case FPR  {_fmt(g.worst_case_fpr)}  (missing negatives counted as false positives)",
        f"    F1              {f1}",
    ]
    if g.fp_upper_bound is not None:
        lines.append(
            f"    FP upper bound  {g.false_positives}/{g.judged_negatives} judged negatives; "
            f"one-sided 95% upper bound {g.fp_upper_bound:.3f}"
        )
    if g.note:
        lines.append(f"    note: {g.note}")
    return lines


def render_text(report: Report) -> str:
    version = report.candidate_version or "n/a"
    ex = report.exclusions
    lines = [
        f"BENCHMARK REPORT [{report.mode}]",
        f"candidate={report.candidate} version={version} run={report.run}",
        *[f"! {n}" for n in report.notices],
        f"excluded: disputed={ex.disputed} proposed={ex.proposed_not_allowed} "
        f"(of which critical={ex.proposed_critical_not_allowed}); "
        f"proposed included={ex.proposed_included}",
        "", "By case type:",
    ]
    for name, stats in report.overall.items():
        lines += _render_group(name, stats, report.f1_interval.get(name))
    lines += ["", "By case type / primary category:"]
    for name, stats in report.by_category.items():
        lines += _render_group(name, stats)
    lines += ["", "By case type / expected judgment:"]
    for name, stats in report.by_expected.items():
        lines += _render_group(name, stats)
    return "\n".join(lines)


def to_dict(report: Report) -> dict:
    return asdict(report)
