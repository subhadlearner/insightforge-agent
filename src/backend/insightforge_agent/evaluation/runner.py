"""Runs the sample briefs through the production pipeline and measures what it persisted.

Every Run goes through `run_brief`, the same check, graph and stores as a submitted Brief, with
the Deps the caller built. Each sample has its own User (`eval-<sample id>`), so one brief's
Reports and Run summaries never become another's memory or Planner context; a memory sample's
setup Runs share its User, which is what makes their Reports eligible memory. Metrics are read
back from the stored Report, never from the objects a Run returned."""

from pathlib import Path

from langchain_core.language_models import BaseChatModel

from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.models import Run, RunState
from insightforge_agent.evaluation.judge import judge_synthesis, not_judged
from insightforge_agent.evaluation.library import SampleBrief
from insightforge_agent.evaluation.metrics import MetricError, citation_metrics, fact_check_metrics
from insightforge_agent.evaluation.report import FactCheckPaths, SampleResult
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.graph import run_brief

NO_JUDGE = "no judge was configured (--judge-provider none)"


def owner_for(sample: SampleBrief) -> str:
    return f"eval-{sample.id}"


def failure_reason(deps: Deps, run: Run) -> str:
    """The last `run_failed` reason in the Run log, or the state the Run ended in."""
    reason, cursor = "", 0
    while batch := deps.repos.events.read_after(run.owner_id, run.id, cursor):
        reason = next((e.payload.get("reason", "") for e in reversed(batch)
                       if e.type == "run_failed"), reason)
        cursor = batch[-1].seq
    return reason or f"the Run ended in {run.state.value}"


def evaluate_sample(deps: Deps, sample: SampleBrief, root: Path,
                    judge: BaseChatModel | None) -> SampleResult:
    owner = owner_for(sample)
    result = SampleResult(sample_id=sample.id, industry=sample.industry,
                          question_type=sample.question_type, status="error", owner_id=owner,
                          synthesis=not_judged("the Run did not complete"))
    if sample.setup is not None:
        for _ in range(sample.setup.runs_before):
            try:
                setup = run_brief(deps, owner, sample.setup.to_brief())
            except Exception as e:  # noqa: BLE001 - recorded per sample; the others still run
                return result.model_copy(update={
                    "status": "setup_failed", "reason": f"setup Run: {type(e).__name__}: {e}"})
            result.setup_run_ids.append(setup.id)
            if setup.state != RunState.COMPLETE:
                return result.model_copy(update={
                    "status": "setup_failed",
                    "reason": f"setup Run {setup.id}: {failure_reason(deps, setup)}"})
    try:
        uploads = sample.uploads(root)
        run = run_brief(deps, owner, sample.to_brief(uploads), uploads=uploads or None)
    except SubmissionRejected as e:
        return result.model_copy(update={"status": "rejected", "reason": str(e)})
    except Exception as e:  # noqa: BLE001
        return result.model_copy(update={"reason": f"{type(e).__name__}: {e}"})
    result.run_id = run.id
    if run.state != RunState.COMPLETE:
        status = "failed" if run.state == RunState.FAILED else "error"
        return result.model_copy(update={"status": status, "reason": failure_reason(deps, run)})
    try:
        report = deps.repos.reports.get_for_run(owner, run.id)
    except Exception as e:  # noqa: BLE001 - the other samples are still measured and written
        return result.model_copy(update={
            "reason": f"the Run is COMPLETE but its Report could not be read: "
                      f"{type(e).__name__}: {e}"})
    result.status, result.report_id = "complete", report.id
    try:
        result.fact_check = fact_check_metrics(report)
    except MetricError as e:
        result.metric_errors.append(str(e))
    attempts, cursor = [], 0
    while batch := deps.repos.events.read_after(owner, run.id, cursor):
        attempts += [e.payload for e in batch if e.type == "fact_check_counterpart"]
        cursor = batch[-1].seq
    result.fact_check_paths = FactCheckPaths(
        counterpart_looked_up=len(attempts),
        counterpart_judged=sum(bool(a.get("entered_top_k")) for a in attempts),
        verified_by_counterpart=sum(bool(a.get("verified_by_counterpart")) for a in attempts))
    try:
        result.citations = citation_metrics(deps.store, owner, report)
    except MetricError as e:
        result.metric_errors.append(str(e))
    result.synthesis = (judge_synthesis(judge, sample, report) if judge is not None
                        else not_judged(NO_JUDGE))
    return result
