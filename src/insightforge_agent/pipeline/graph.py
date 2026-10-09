"""The outer LangGraph (ADR-0001): PLANNING -> RESEARCHING -> (repair) -> EXTRACTING ->
SYNTHESIZING -> WRITING -> FACT_CHECKING -> COMPLETE.

Graph state holds IDs and small structured values only. Source bodies and Passage text stay
in the SourceStore and are read by the stage that needs them, one bounded batch at a time.
INGESTING is skipped (no uploaded documents yet); FACT_CHECKING only passes the draft through."""

import uuid
from collections.abc import Callable
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from insightforge_agent.agents.planner import build_planner, submitted_plan
from insightforge_agent.agents.researcher import (
    SUMMARY_EVENT,
    make_web_search_tool,
    researcher_subagent,
    source_ids_in,
)
from insightforge_agent.domain.contracts import (
    Brief,
    EvidenceBundle,
    ExtractionResult,
    ReportDraft,
    ResearchResults,
    SourceRef,
    SubTaskResult,
    VerifiedReport,
)
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import (
    Report,
    Run,
    RunState,
    SubTaskRecord,
    SubTaskStatus,
)
from insightforge_agent.domain.plan import MAX_ASPECTS, SubTaskPlan, plan_violations, trim_plan
from insightforge_agent.domain.submission import check_submission
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.dispatch import dispatches, missing_ids, resolve, segments
from insightforge_agent.pipeline.errors import RunFailed
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.synthesize import synthesize
from insightforge_agent.pipeline.write import write_report

# Source types a Run can actually carry out so far; documents and memory arrive in T7 and T8.
IMPLEMENTED_SOURCE_TYPES = ["web"]


class State(TypedDict, total=False):
    thread_id: str
    owner_id: str
    run_id: str
    brief: str
    allowed_types: list[str]
    plan: dict
    repaired: bool
    missing: list[str]
    research: dict
    extraction: dict
    bundle: dict
    draft: dict
    verified: dict
    report_id: str


def summaries_from_log(deps: Deps, owner_id: str, run_id: str) -> dict[str, SourceRef]:
    """The one-line summaries the fetch tool recorded, read back from the durable Run log."""
    found: dict[str, SourceRef] = {}
    cursor = 0
    while batch := deps.repos.events.read_after(owner_id, run_id, cursor):
        for e in batch:
            if e.type == SUMMARY_EVENT:
                ref = SourceRef.model_validate(e.payload)
                found[ref.source_id] = ref
        cursor = batch[-1].seq
    return found


def outer_config(thread_id: str) -> dict:
    """The outer graph checkpoints under its own thread, apart from the Planner's."""
    return {"configurable": {"thread_id": f"graph-{thread_id}"}}


def build_graph(deps: Deps, interrupt_after: list[str] | None = None):
    """Compile the outer graph for one Run, checkpointed so its state survives a restart."""
    # The Researcher tool owns one Run's ids, so the Planner is built per Run, on first use.
    holder: dict = {}

    def planner_for(state: State):
        if "planner" not in holder:
            tool = make_web_search_tool(
                owner_id=state["owner_id"], run_id=state["run_id"], search=deps.search,
                fetcher=deps.fetcher, store=deps.store, summarizer=deps.light_model,
                events=deps.repos.events, now=deps.now,
                passage_token_cap=deps.passage_token_cap,
            )
            holder["planner"] = build_planner(
                model=deps.planner_model,
                subagents=[researcher_subagent([tool], deps.researcher_model)],
                checkpointer=deps.checkpointer,
            )
        return holder["planner"]

    def cfg(state: State) -> dict:
        return {"configurable": {"thread_id": state["thread_id"]},
                "recursion_limit": deps.recursion_limit}

    def enter(state: State, stage: RunState) -> None:
        deps.repos.runs.set_state(state["owner_id"], state["run_id"], stage)
        deps.log(state["owner_id"], state["run_id"], "stage", state=stage.value)

    def thread(state: State) -> list:
        return planner_for(state).get_state(cfg(state)).values["messages"]

    def planning(state: State) -> State:
        enter(state, RunState.PLANNING)
        owner, run_id = state["owner_id"], state["run_id"]
        allowed = state.get("allowed_types", IMPLEMENTED_SOURCE_TYPES)

        def attempt(prompt: str) -> tuple[SubTaskPlan | None, list[str]]:
            res = planner_for(state).invoke({"messages": [HumanMessage(prompt)]}, cfg(state))
            try:
                plan = submitted_plan(res["messages"])
            except ValueError as e:
                return None, [f"planning produced no valid plan: {e}"]
            return plan, plan_violations(plan, allowed)

        plan, problems = attempt(
            f"PLAN. Allowed source types: {', '.join(allowed)}. Brief: {state['brief']}")
        if problems:
            too_many_aspects = plan is not None and len(plan.aspects) > MAX_ASPECTS
            deps.log(owner, run_id, "plan_correction", violations=problems)
            rules = "\n".join(f"- {p}" for p in problems)
            ask = (f"PLAN (correction). Your plan broke these rules:\n{rules}\n"
                   "Call submit_plan again with a corrected plan.")
            if too_many_aspects:
                ask += (" Consolidate genuinely related Aspects. Never drop an Aspect the Brief "
                        "asked about.")
            plan, problems = attempt(ask)
            if plan is not None:
                plan, trimmed = trim_plan(plan)
                if trimmed:
                    deps.log(owner, run_id, "plan_trimmed", dropped=trimmed)
                problems = plan_violations(plan, allowed)
        if plan is None or problems:
            raise RunFailed("; ".join(problems))
        deps.repos.subtasks.save_plan(owner, run_id, [
            SubTaskRecord(run_id=run_id, subtask=t) for t in plan.sub_tasks])
        return {"plan": plan.model_dump(), "repaired": False}

    def researching(state: State) -> State:
        enter(state, RunState.RESEARCHING)
        approved = "\n".join(f"- {t['id']}: {t['query']}" for t in state["plan"]["sub_tasks"])
        planner_for(state).invoke({"messages": [HumanMessage(
            f"RESEARCH: dispatch exactly these approved Sub-tasks now:\n{approved}")]}, cfg(state))
        return {}

    def outcomes(state: State):
        segs = segments(thread(state))
        ids = [t["id"] for t in state["plan"]["sub_tasks"]]
        return resolve(ids, dispatches(segs.get("RESEARCH", [])),
                       dispatches(segs.get("REPAIR", [])) if state["repaired"] else [])

    def verify(state: State) -> State:
        return {"missing": missing_ids(outcomes(state))}

    def repair(state: State) -> State:
        by_id = {t["id"]: t for t in state["plan"]["sub_tasks"]}
        listing = "\n".join(f"- {i}: {by_id[i]['query']}" for i in state["missing"])
        deps.log(state["owner_id"], state["run_id"], "repair", missing=state["missing"])
        planner_for(state).invoke({"messages": [HumanMessage(
            f"REPAIR: dispatch only these missing Sub-tasks:\n{listing}")]}, cfg(state))
        return {"repaired": True}

    def collect(state: State) -> State:
        owner, run_id = state["owner_id"], state["run_id"]
        known = {s.id for s in deps.repos.sources.list_for_run(owner, run_id)}
        summaries = summaries_from_log(deps, owner, run_id)
        results = []
        for sid, outcome in outcomes(state).items():
            refs: list[SourceRef] = []
            if outcome.status == "succeeded":
                for src in source_ids_in(outcome.dispatch.result or ""):
                    ref = summaries.get(src)
                    if src in known and ref is not None:
                        refs.append(ref)
            ok = bool(refs)
            if not ok:
                deps.log(owner, run_id, "subtask_failed", subtask_id=sid, status=outcome.status)
            results.append(SubTaskResult(
                subtask_id=sid, status="succeeded" if ok else "failed", source_refs=refs))
            deps.repos.subtasks.set_status(
                owner, run_id, sid, SubTaskStatus.SUCCEEDED if ok else SubTaskStatus.FAILED)
        if not any(r.status == "succeeded" for r in results):
            raise RunFailed("every Sub-task failed")
        return {"research": ResearchResults(results=results).model_dump()}

    def extracting(state: State) -> State:
        enter(state, RunState.EXTRACTING)
        research = ResearchResults.model_validate(state["research"])
        source_ids = list(dict.fromkeys(
            ref.source_id for r in research.results for ref in r.source_refs))
        result = extract(deps, state["owner_id"], state["run_id"], source_ids)
        return {"extraction": result.model_dump()}

    def synthesizing(state: State) -> State:
        enter(state, RunState.SYNTHESIZING)
        bundle = synthesize(
            deps, state["owner_id"], state["run_id"], SubTaskPlan.model_validate(state["plan"]),
            ResearchResults.model_validate(state["research"]),
            ExtractionResult.model_validate(state["extraction"]),
        )
        if not bundle.sections:
            raise RunFailed("no Evidence item survived synthesis")
        return {"bundle": bundle.model_dump()}

    def writing(state: State) -> State:
        enter(state, RunState.WRITING)
        research = ResearchResults.model_validate(state["research"])
        by_id = {t.id: t for t in SubTaskPlan.model_validate(state["plan"]).sub_tasks}
        gaps = [f"Sub-task {r.subtask_id} ({by_id[r.subtask_id].query}) produced no usable sources."
                for r in research.results if r.status == "failed"]
        bundle = EvidenceBundle.model_validate(state["bundle"])
        draft = write_report(deps, state["owner_id"], state["run_id"], state["brief"], bundle, gaps)
        return {"draft": draft.model_dump()}

    def fact_checking(state: State) -> State:
        enter(state, RunState.FACT_CHECKING)
        verified = fact_check(ReportDraft.model_validate(state["draft"]))
        return {"verified": verified.model_dump()}

    def complete(state: State) -> State:
        owner, run_id = state["owner_id"], state["run_id"]
        draft = ReportDraft.model_validate(state["draft"])
        verified = VerifiedReport.model_validate(state["verified"])
        try:
            # A retry after a crash between storing the Report and marking COMPLETE reuses it.
            report = deps.repos.reports.get_for_run(owner, run_id)
        except NotFoundError:
            report = Report(
                id=f"rep_{deps.new_id()}", owner_id=owner, run_id=run_id, created_at=deps.now(),
                body={"brief": state["brief"], "draft": draft.model_dump(),
                      "verified": verified.model_dump(), "evidence": state["bundle"]},
            )
            deps.repos.reports.add(report)
        enter(state, RunState.COMPLETE)
        return {"report_id": report.id}

    g = StateGraph(State)
    for name, fn in [("planning", planning), ("researching", researching), ("verify", verify),
                     ("repair", repair), ("collect", collect), ("extracting", extracting),
                     ("synthesizing", synthesizing), ("writing", writing), ("fact_checking", fact_checking),
                     ("complete", complete)]:
        g.add_node(name, fn)
    g.add_edge(START, "planning")
    g.add_edge("planning", "researching")
    g.add_edge("researching", "verify")
    g.add_conditional_edges(
        "verify", lambda s: "repair" if s["missing"] and not s["repaired"] else "collect",
        {"repair": "repair", "collect": "collect"})
    g.add_edge("repair", "verify")
    g.add_edge("collect", "extracting")
    g.add_edge("extracting", "synthesizing")
    g.add_edge("synthesizing", "writing")
    g.add_edge("writing", "fact_checking")
    g.add_edge("fact_checking", "complete")
    g.add_edge("complete", END)
    return g.compile(checkpointer=deps.checkpointer, interrupt_after=interrupt_after)


def run_brief(
    deps: Deps, owner_id: str, brief: Brief, on_state: Callable[[dict], None] | None = None,
) -> Run:
    """Create a Run for the Brief and carry it to COMPLETE or FAILED. A Brief with no available
    source type raises SubmissionRejected before any Run exists. A Run that fails does not raise; its reason is in the Run log. `on_state` sees every graph state snapshot."""
    allowed = [t for t in check_submission(
        brief, bool(deps.repos.reports.list_for_owner(owner_id))) if t in IMPLEMENTED_SOURCE_TYPES]
    run = Run(id=f"run_{deps.new_id()}", owner_id=owner_id, brief=brief.text,
              state=RunState.PLANNING, created_at=deps.now())
    deps.repos.runs.add(run)
    initial: State = {"thread_id": f"thread-{uuid.uuid4()}", "owner_id": owner_id,
                      "run_id": run.id, "brief": brief.text,
                      "allowed_types": allowed}
    try:
        for snapshot in build_graph(deps).stream(
                initial, outer_config(initial["thread_id"]), stream_mode="values"):
            if on_state:
                on_state(snapshot)
    except Exception as e:  # noqa: BLE001 - any failure ends the Run, with its reason logged
        reason = str(e) if isinstance(e, RunFailed) else f"{type(e).__name__}: {e}"
        try:
            # A Run that already stored its Report is complete, never FAILED with a Report.
            deps.repos.reports.get_for_run(owner_id, run.id)
        except NotFoundError:
            deps.log(owner_id, run.id, "run_failed", reason=reason)
            return deps.repos.runs.set_state(owner_id, run.id, RunState.FAILED)
        deps.log(owner_id, run.id, "completion_recovered", detail=reason)
        return deps.repos.runs.set_state(owner_id, run.id, RunState.COMPLETE)
    return deps.repos.runs.get(owner_id, run.id)
