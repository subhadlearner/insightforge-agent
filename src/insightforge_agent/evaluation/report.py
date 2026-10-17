"""The evaluation report: what ran, under which conditions, and what each provider measured.

Results are recorded per provider, each with the conditions its Runs ran under. A brief whose
Run did not complete is not measured: it shows its status and reason and is left out of every
total, never scored as zero. Comparability is decided metric by metric: two providers' figures
for a metric are comparable only when their conditions match and the metric was measured on
the same briefs (one judge scores every provider of an evaluation)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from insightforge_agent.evaluation.judge import SynthesisResult
from insightforge_agent.evaluation.library import LIBRARY_SIZE
from insightforge_agent.evaluation.metrics import (
    CitationMetrics,
    FactCheckMetrics,
    pool_citations,
    pool_fact_checks,
)

SCHEMA_VERSION = "insightforge-eval/1"
PASS_RATE_THRESHOLD = 0.8  # design.md section 11: below it, the Anthropic run needs remediation
REMEDIATION_PROVIDER = "anthropic"  # section 11 names the Anthropic run

SampleStatus = Literal["complete", "failed", "rejected", "setup_failed", "error"]
Setting = str | int | float | bool | None


class SampleResult(BaseModel):
    sample_id: str
    industry: str
    question_type: str
    status: SampleStatus
    owner_id: str
    run_id: str | None = None
    setup_run_ids: list[str] = []
    report_id: str | None = None
    reason: str = ""  # why the Run did not complete
    fact_check: FactCheckMetrics | None = None
    citations: CitationMetrics | None = None
    synthesis: SynthesisResult
    metric_errors: list[str] = []


class SynthesisTotals(BaseModel):
    judged: int = 0
    errors: int = 0
    not_judged: int = 0
    mean_score: float | None = None  # over judged Reports only


class Totals(BaseModel):
    samples: int
    complete: int
    fact_check: FactCheckMetrics  # pooled over the complete Reports that could be measured
    citations: CitationMetrics
    synthesis: SynthesisTotals


class Remediation(BaseModel):
    threshold: float = PASS_RATE_THRESHOLD
    status: Literal["required", "not_required", "undetermined", "not_applicable"]
    reason: str


class ProviderResult(BaseModel):
    provider: str
    configured_models: dict[str, str]  # per role, from Settings: what build_chat_model builds
    runtime_models: dict[str, str]  # per role, the chat model class and model id that ran
    runtime_services: dict[str, str]  # the embedder, search and fetcher classes that ran
    conditions: dict[str, Setting]  # the pipeline settings these Runs ran under
    workdir: str  # the SQLite files holding these Runs, Reports and Run logs
    samples: list[SampleResult]
    totals: Totals
    remediation: Remediation


class LibraryInfo(BaseModel):
    directory: str
    briefs_in_library: int
    sample_ids: list[str]  # the briefs this evaluation ran, in order
    fingerprint: str  # library_fingerprint: every brief file and document

    @property
    def full(self) -> bool:
        """Every brief of the complete sample library was run."""
        return len(self.sample_ids) == self.briefs_in_library == LIBRARY_SIZE


class CodeVersion(BaseModel):
    git_commit: str | None
    git_dirty: bool | None  # uncommitted changes; None when git could not tell


class JudgeIdentity(BaseModel):
    provider: str  # a chat provider, or "none"
    rubric_version: str
    model: str | None = None  # the configured model id
    runtime_model: str | None = None  # the class and model id that ran
    role: str | None = None  # whose Settings model the judge is
    inference: dict[str, Setting] | None = None  # set on the model; anything absent is default


class Comparison(BaseModel):
    comparable: bool
    reason: str


class EvalReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime
    library: LibraryInfo
    code: CodeVersion
    judge: JudgeIdentity
    providers: list[ProviderResult]
    comparability: dict[str, Comparison] = {}  # per metric, when more than one provider ran


def totals_of(samples: list[SampleResult]) -> Totals:
    judged = [s.synthesis.score for s in samples if s.synthesis.status == "judged"]
    return Totals(
        samples=len(samples),
        complete=sum(s.status == "complete" for s in samples),
        fact_check=pool_fact_checks([s.fact_check for s in samples if s.fact_check]),
        citations=pool_citations([s.citations for s in samples if s.citations]),
        synthesis=SynthesisTotals(
            judged=len(judged),
            errors=sum(s.synthesis.status == "error" for s in samples),
            not_judged=sum(s.synthesis.status == "not_judged" for s in samples),
            mean_score=sum(judged) / len(judged) if judged else None))


def remediation_of(provider: str, samples: list[SampleResult], totals: Totals,
                   library: LibraryInfo) -> Remediation:
    """Section 11 applies to the Anthropic run of the whole library: a partial run is never
    that run, so it is undetermined whatever it measured (its reason still says the rate)."""
    rate = totals.fact_check.pass_rate
    shown = "none" if rate is None else f"{rate:.1%}"
    below = rate is not None and rate < PASS_RATE_THRESHOLD
    if provider != REMEDIATION_PROVIDER:
        return Remediation(status="not_applicable",
                           reason=f"the threshold applies to the Anthropic run; pass rate {shown}")
    if not library.full:
        return Remediation(status="undetermined", reason=(
            f"partial run ({len(samples)} of the {LIBRARY_SIZE}-brief library); pass rate "
            f"{shown}" + (f", below {PASS_RATE_THRESHOLD:.0%}" if below else "")
            + "; section 11 is decided on the full Anthropic run"))
    if below:
        return Remediation(status="required", reason=(
            f"pass rate {shown} is below {PASS_RATE_THRESHOLD:.0%}: a documented remediation "
            "attempt is required (design.md section 11)"))
    unmeasured = [s.sample_id for s in samples if s.fact_check is None]
    if rate is None or unmeasured:
        return Remediation(status="undetermined", reason=(
            f"pass rate {shown} over {len(samples) - len(unmeasured)} of {len(samples)} briefs; "
            f"not measured: {', '.join(unmeasured) or 'none'}"))
    return Remediation(status="not_required",
                       reason=f"pass rate {shown} meets {PASS_RATE_THRESHOLD:.0%}")


def _measured(result: ProviderResult, metric: str) -> frozenset[str]:
    if metric == "synthesis":
        return frozenset(s.sample_id for s in result.samples if s.synthesis.status == "judged")
    return frozenset(s.sample_id for s in result.samples if getattr(s, metric) is not None)


METRICS = ("fact_check", "citations", "synthesis")


def comparability_of(providers: list[ProviderResult]) -> dict[str, Comparison]:
    """The judge is shared by construction; the conditions and the briefs each metric was
    measured on can still differ between providers."""
    if len(providers) < 2:
        return {}
    first = providers[0].conditions
    differing = sorted(k for p in providers[1:] for k in first.keys() | p.conditions.keys()
                       if first.get(k) != p.conditions.get(k))
    if differing:
        reason = f"the providers ran under different conditions ({', '.join(differing)})"
        return {m: Comparison(comparable=False, reason=reason) for m in METRICS}
    out = {}
    for metric in METRICS:
        sets = {p.provider: _measured(p, metric) for p in providers}
        every = frozenset().union(*sets.values())
        if not every:
            out[metric] = Comparison(comparable=False, reason="no provider was measured")
        elif all(s == every for s in sets.values()):
            out[metric] = Comparison(comparable=True, reason=(
                f"measured on the same {len(every)} brief(s) for every provider"))
        else:
            detail = "; ".join(f"{p} has none for {', '.join(sorted(every - s))}"
                               for p, s in sets.items() if every - s)
            out[metric] = Comparison(comparable=False, reason=(
                f"measured on different briefs ({detail}); compare per brief instead"))
    return out


# --- Markdown ------------------------------------------------------------------------------


def definitions(judge: JudgeIdentity, conditions: dict[str, Setting]) -> str:
    rate, minimum = conditions.get("fact_check_rate"), conditions.get("fact_check_minimum")
    sampling = (f"{rate:.0%}, at least {minimum}" if isinstance(rate, float) else "as configured")
    return f"""\
## Metric definitions

- **Fact-check pass rate** (deterministic): the Fact-Checker's persisted verdicts,
  `verified / (verified + unverified)` over its sampled Claims ({sampling}, seeded by the
  Run). `unchecked` Claims (re-fetch failed) are left out. Totals pool Claims across Reports.
- **Citation accuracy** (deterministic): every Claim, sampled or not. Accurate when the
  citation resolves (Claim -> Evidence or Historical item -> primary Passage -> Source) and that
  Passage states every figure of the Claim. Unresolved Claims count as inaccurate. This checks
  provenance and figures only, not that the Passage semantically supports the Claim.
- **Synthesis quality** (LLM judge, rubric `{judge.rubric_version}`): coverage, organisation,
  synthesis and calibration, each 1 to 5; the score is their mean. It never judges accuracy or
  citations. A failed or invalid judge reply is an error, never a score.
- A brief whose Run did not complete is **not measured**: it is listed with its reason and left
  out of every total, never counted as zero.
- The section 11 rule: a fact-check pass rate under {PASS_RATE_THRESHOLD:.0%} on the Anthropic
  run of the full library requires a documented remediation attempt.

## Limitations

- One Run per brief per provider: differences between providers may be run-to-run noise.
- The Fact-Checker samples Claims, so the pass rate rests on few Claims per Report.
- Clarification is off (no timeout scheduler): a Planner question is refused and the Brief is
  planned as given, as for a Watchlist Run.
- Runs use an in-process vector store and their own SQLite files, never the app's database.
"""


def _format_rate(value: float | None, num: int, den: int, none: str) -> str:
    return none if value is None else f"{value:.1%} ({num}/{den})"


def _fact(m: FactCheckMetrics | None) -> str:
    if m is None:
        return "not measured"
    text = _format_rate(m.pass_rate, m.verified, m.checked, "n/a (nothing checkable)")
    return text + (f", {m.unchecked} unchecked" if m.unchecked else "")


def _cite(m: CitationMetrics | None) -> str:
    if m is None:
        return "not measured"
    return _format_rate(m.accuracy, m.accurate, m.claims, "n/a (no Claims)")


def _synth(s: SynthesisResult) -> str:
    if s.status == "judged":
        return f"{s.score:.2f} / 5"
    return s.status.replace("_", " ")


def _settings(values: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in values.items())


def render_markdown(report: EvalReport) -> str:
    lib, judge = report.library, report.judge
    lines = [
        "# InsightForge evaluation report", "",
        f"Generated {report.generated_at.isoformat()} · schema `{report.schema_version}`", "",
        "## What ran", "",
        f"- Library: `{lib.directory}`, {len(lib.sample_ids)} brief(s) run of "
        f"{lib.briefs_in_library} (fingerprint `{lib.fingerprint[:16]}`)",
        f"- Code: `{report.code.git_commit or 'unknown'}`"
        + (" (uncommitted changes)" if report.code.git_dirty else ""),
        f"- Providers: {', '.join(p.provider for p in report.providers)}",
        f"- Judge: {judge.provider}" + (f" `{judge.model}`" if judge.model else "")
        + f", rubric `{judge.rubric_version}`"
        + (f", inference {_settings(judge.inference) or 'provider defaults'}"
           if judge.inference is not None else ""), "",
    ]
    for p in report.providers:
        t = p.totals
        lines += [
            f"## Provider: {p.provider}", "",
            "Models: " + ", ".join(f"{role} `{name}`" for role, name in p.runtime_models.items()),
            "",
            "Services: " + ", ".join(f"{k} `{v}`" for k, v in p.runtime_services.items()), "",
            f"Conditions: {_settings(p.conditions)}", "",
            "| Brief | Status | Fact-check pass rate | Citation accuracy | Synthesis |",
            "|---|---|---|---|---|",
        ]
        for s in p.samples:
            status = s.status if not s.reason else f"{s.status}: {s.reason[:120]}"
            lines.append(f"| {s.sample_id} | {status} | {_fact(s.fact_check)} | "
                         f"{_cite(s.citations)} | {_synth(s.synthesis)} |")
        mean = t.synthesis.mean_score
        lines += [
            f"| **Total** ({t.complete} of {t.samples} complete) | | {_fact(t.fact_check)} | "
            f"{_cite(t.citations)} | "
            + (f"{mean:.2f} / 5 over {t.synthesis.judged}" if mean is not None else "not judged")
            + " |", "",
            f"Remediation (section 11): **{p.remediation.status}**. {p.remediation.reason}.", "",
        ]
        problems = [(s.sample_id, x) for s in p.samples
                    for x in (s.metric_errors + (s.citations.problems if s.citations else [])
                              + ([s.synthesis.reason] if s.synthesis.status == "error" else []))]
        if problems:
            lines += ["Problems:", ""] + [f"- {sid}: {x}" for sid, x in problems] + [""]
    if report.comparability:
        lines += ["## Provider comparability", ""]
        lines += [f"- {metric}: **{'comparable' if c.comparable else 'not comparable'}**, "
                  f"{c.reason}" for metric, c in report.comparability.items()]
        lines.append("")
    lines.append(definitions(judge, report.providers[0].conditions))
    return "\n".join(lines)
