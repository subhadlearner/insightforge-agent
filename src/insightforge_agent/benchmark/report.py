"""Reports: counts, numerators, denominators and intervals per case type, category and expected
judgment. Predictions are joined to reviewed ground truth at read time; nothing here writes
either side. Denominator rules are documented in docs/benchmark/methodology.md."""

from collections import Counter
from dataclasses import asdict, dataclass, field

from insightforge_agent.benchmark.cases import (
    POSITIVE_LABEL,
    BenchmarkCase,
    CaseType,
    GroundInputs,
    InputAccess,
    LabelStatus,
    PairLabel,
    SupportLabel,
)
from insightforge_agent.benchmark.judge import DISPOSITION_KEY, REASON_KEY, JudgeStatus
from insightforge_agent.benchmark.recording import RecordingError, RecordingStore, case_hash
from insightforge_agent.benchmark.stats import (
    Interval,
    Outcome,
    clopper_pearson_upper,
    f1_score,
    paired_bootstrap_f1,
    wilson_interval,
)

NOT_RECORDED = "NOT_RECORDED"
STALE = "STALE"  # recorded, but for a case that has since changed: never used, counted as missing


class MixedCandidateVersions(RecordingError):
    """One run holds recordings from different candidate versions, models or configurations."""
# The label whose wrong prediction is a false contradiction, per case type.
CONTRADICTION_LABEL = {CaseType.PAIR: PairLabel.CONTRADICTORY,
                       CaseType.SUPPORT: SupportLabel.CONTRADICTED}

NOT_EVALUATED = [
    "False-HIGH Confidence is not evaluated: it depends on source credibility and the full "
    "Confidence rule, which benchmark cases do not carry. INDEPENDENCE cases measure only "
    "false independence of two Sources.",
    "Budget-cut Evidence is not evaluated: a case holds one item, not a ranked bundle held to a "
    "token budget, so no item can be cut.",
]
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
    stale: int
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
    # PAIR and SUPPORT only: a predicted CONTRADICTORY / CONTRADICTED whose expected label differs,
    # over judged cases not expected to be a contradiction. Separate from false merge and false
    # support (the positive class), which it never feeds.
    false_contradiction: Proportion | None = None
    # Why a prediction is missing, and what the candidate's own Evidence pipeline would do to the
    # item under test (drafted = every recorded case that reports a disposition). Counts only;
    # a candidate that reports none leaves these empty.
    unjudged_reasons: dict[str, int] = field(default_factory=dict)
    dispositions: dict[str, int] = field(default_factory=dict)
    disposition_reasons: dict[str, int] = field(default_factory=dict)


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
    access: str = InputAccess.SEMANTIC.value  # the input view the predictions must match
    by_ground_check: dict[str, GroupStats] = field(default_factory=dict)
    not_evaluated: list[str] = field(default_factory=lambda: list(NOT_EVALUATED))


@dataclass(frozen=True)
class _Row:
    case: BenchmarkCase
    status: str
    predicted: object | None  # the label if JUDGED
    reason: str | None = None
    diagnostics: dict | None = None

    @property
    def outcome(self) -> Outcome:
        positive = POSITIVE_LABEL[self.case.type]
        judged = self.status == JudgeStatus.JUDGED
        return Outcome(self.case.expected == positive,
                       (self.predicted == positive) if judged else None,
                       expected_label=self.case.expected.value)


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
    outcomes = [r.outcome for r in rows]
    contradiction = CONTRADICTION_LABEL.get(rows[0].case.type) if rows else None
    false_contradiction = None
    if contradiction is not None:
        judged_rows = [r for r in rows if r.status == JudgeStatus.JUDGED
                       and r.case.expected != contradiction]
        false_contradiction = proportion(
            sum(r.predicted == contradiction for r in judged_rows), len(judged_rows))
    unjudged = Counter(f"{r.status}: {r.reason}" for r in rows if r.reason and r.status in (
        JudgeStatus.ABSTAIN, JudgeStatus.ERROR, JudgeStatus.NOT_APPLICABLE))
    reported = [r.diagnostics for r in rows if r.diagnostics and DISPOSITION_KEY in r.diagnostics]
    return GroupStats(
        false_contradiction=false_contradiction, unjudged_reasons=dict(unjudged),
        dispositions=dict(Counter(d[DISPOSITION_KEY] for d in reported)),
        disposition_reasons=dict(Counter(d[REASON_KEY] for d in reported if REASON_KEY in d)),
        eligible=eligible, judged=counts[JudgeStatus.JUDGED], abstain=counts[JudgeStatus.ABSTAIN],
        error=counts[JudgeStatus.ERROR], not_applicable=counts[JudgeStatus.NOT_APPLICABLE],
        not_recorded=counts[NOT_RECORDED], stale=counts[STALE],
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
    *, allow_proposed: bool = False, candidate_version: str | None = None,
    access: InputAccess = InputAccess.SEMANTIC,
) -> Report:
    """Join recorded predictions to reviewed labels. Recordings whose case has changed are
    STALE (excluded, counted as missing). A run mixing candidate versions, models or
    configurations is refused unless `candidate_version` selects one version, and even then
    only one (model, configuration) may remain. A recording counts only if it was made for the
    `access` view of the case (SEMANTIC by default): a BASELINE recording is stale for a SEMANTIC
    report and the other way round, so the two views are never accepted interchangeably."""
    everything = store.records_for(candidate, run)
    chosen = [r for r in everything if candidate_version in (None, r.candidate_version)]
    identities = {(r.candidate_version, r.model, r.config_hash) for r in chosen}
    if len(identities) > 1:
        raise MixedCandidateVersions(
            f"run {run!r} of {candidate!r} mixes {len(identities)} candidate "
            f"version/model/configuration combinations: {sorted(identities)}; "
            "pass --candidate-version, or use a new run id per version"
        )
    other_version = len(everything) - len(chosen)
    records = {r.case_id: r for r in chosen}
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
        fresh = case_hash(case.prediction_view(access))
        if record is None:
            rows.append(_Row(case, NOT_RECORDED, None))
        elif record.case_hash != fresh:
            rows.append(_Row(case, STALE, None))
        else:
            verdict = record.verdict
            rows.append(_Row(case, verdict.status.value, verdict.label, verdict.reason,
                             verdict.diagnostics))

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
    notices.append(f"Predictions must match the {access.value} input view of each case.")
    stale_total = sum(r.status == STALE for r in rows)
    if stale_total:
        notices.append(f"{stale_total} recording(s) are stale (the case changed since it was "
                       "recorded); they are excluded and counted as missing predictions.")
    if other_version:
        notices.append(f"{other_version} recording(s) from other candidate versions were "
                       "excluded by --candidate-version.")

    report = Report(
        mode="EXPLORATORY" if exploratory else "QUALIFICATION", candidate=candidate,
        candidate_version=next(iter(records.values())).candidate_version if records else None,
        run=run, exclusions=Exclusions(disputed, proposed_out, critical_out, proposed_in),
        notices=notices, access=access.value,
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
        if case_type is CaseType.GROUND:
            checks = sorted({r.case.inputs.check.value for r in of_type
                             if isinstance(r.case.inputs, GroundInputs)})
            for check in checks:
                report.by_ground_check[f"GROUND/{check}"] = group_stats(
                    [r for r in of_type if r.case.inputs.check.value == check])
            if len(checks) > 1:
                notices.append("GROUND combines two different checks (QUOTED_SPAN and "
                               "CLAIM_PASSAGE); read them separately, not the combined figure.")
        for expected in sorted({r.case.expected.value for r in of_type}):
            report.by_expected[f"{case_type.value}/{expected}"] = group_stats(
                [r for r in of_type if r.case.expected.value == expected])
    return report


def _f1_interval(rows: list[_Row]) -> Interval | None:
    aligned = {r.case.id: r.outcome for r in rows}
    return paired_bootstrap_f1({"candidate": aligned}).candidates["candidate"].interval


def _fmt(p: Proportion) -> str:
    if p.denominator == 0:
        return "undefined (0/0)"
    low, high = p.interval.low, p.interval.high
    return f"{p.numerator}/{p.denominator} = {p.value:.3f} [{low:.3f}, {high:.3f}]"


def _render_group(name: str, g: GroupStats, interval: Interval | None = None) -> list[str]:
    f1 = "undefined" if g.f1 is None else f"{g.f1:.3f}"
    if interval is not None and g.f1 is not None:
        f1 += f" [{interval.low:.3f}, {interval.high:.3f}] (bootstrap)"
    lines = [
        f"  {name}: eligible={g.eligible} judged={g.judged} abstain={g.abstain} error={g.error} "
        f"not_applicable={g.not_applicable} not_recorded={g.not_recorded} stale={g.stale}",
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
    if g.false_contradiction is not None:
        lines.append(f"    false contradiction {_fmt(g.false_contradiction)}  "
                     "(predicted contradiction among judged non-contradictions)")
    for reason, count in sorted(g.unjudged_reasons.items()):
        lines.append(f"    unjudged: {count} x {reason}")
    for name, count in sorted(g.dispositions.items()):
        lines.append(f"    evidence: {count} x {name}")
    for name, count in sorted(g.disposition_reasons.items()):
        lines.append(f"      reason: {count} x {name}")
    if g.note:
        lines.append(f"    note: {g.note}")
    return lines


def render_text(report: Report) -> str:
    version = report.candidate_version or "n/a"
    ex = report.exclusions
    lines = [
        f"BENCHMARK REPORT [{report.mode}]",
        f"candidate={report.candidate} version={version} run={report.run} "
        f"input_access={report.access}",
        *[f"! {n}" for n in report.notices],
        f"excluded: disputed={ex.disputed} proposed={ex.proposed_not_allowed} "
        f"(of which critical={ex.proposed_critical_not_allowed}); "
        f"proposed included={ex.proposed_included}",
        "", "By case type:",
    ]
    for name, stats in report.overall.items():
        lines += _render_group(name, stats, report.f1_interval.get(name))
    if report.by_ground_check:
        lines += ["", "GROUND by check (separate checks; never read together):"]
        for name, stats in report.by_ground_check.items():
            lines += _render_group(name, stats)
    lines += ["", "By case type / primary category:"]
    for name, stats in report.by_category.items():
        lines += _render_group(name, stats)
    lines += ["", "By case type / expected judgment:"]
    for name, stats in report.by_expected.items():
        lines += _render_group(name, stats)
    lines += ["", "Not evaluated:", *[f"  - {n}" for n in report.not_evaluated]]
    return "\n".join(lines)


def to_dict(report: Report) -> dict:
    return asdict(report)
