"""The outer LangGraph (ADR-0001): (INGESTING) -> PLANNING -> RESEARCHING -> (repair) ->
EXTRACTING -> SYNTHESIZING -> WRITING -> FACT_CHECKING -> COMPLETE. A Vague Brief detours
once through `clarify` (AWAITING_CLARIFICATION, a LangGraph interrupt) back into PLANNING.

Graph state holds IDs and small structured values only. Source bodies, uploaded file bytes and
Passage text stay out of it: uploads reach INGESTING through the compiled graph, and text is
read from the SourceStore by the stage that needs it, one bounded batch at a time. INGESTING
runs only when documents were uploaded."""

from collections.abc import Callable
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from insightforge_agent.agents.doc_reader import (
    DOC_SEARCH_EVENT,
    doc_reader_subagent,
    make_doc_search_tool,
)
from insightforge_agent.agents.memory_researcher import (
    MEMORY_SEARCH_EVENT,
    make_memory_search_tool,
    memory_researcher_subagent,
)
from insightforge_agent.agents.planner import (
    ApprovedDispatchGuard,
    asked_question,
    build_planner,
    plan_assumption,
    submitted_plan,
)
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
    HistoricalContext,
    ReportDraft,
    ResearchResults,
    SourceRef,
    SubTaskResult,
    VerifiedReport,
)
from insightforge_agent.domain.documents import Upload, check_uploads, chunk_id, chunk_ids_in
from insightforge_agent.domain.errors import (
    ClarificationConflict,
    NotFoundError,
    SubmissionRejected,
)
from insightforge_agent.domain.ids import derive_report_id
from insightforge_agent.domain.memory import memory_ref_id, memory_refs_in, render_run_summaries
from insightforge_agent.domain.models import (
    Clarification,
    Report,
    Run,
    RunState,
    SubTaskRecord,
    SubTaskStatus,
)
from insightforge_agent.domain.plan import (
    AGENT_FOR_SOURCE_TYPE,
    MAX_ASPECTS,
    SubTaskPlan,
    plan_violations,
    trim_plan,
)
from insightforge_agent.domain.submission import check_submission
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.dispatch import dispatches, missing_ids, resolve, segments
from insightforge_agent.pipeline.errors import RunFailed
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.ingest import ingest_documents
from insightforge_agent.pipeline.memory import (
    eligible_report_ids,
    finalise_report,
    report_finalised,
    resolve_historical,
    summaries_for_planner,
)
from insightforge_agent.pipeline.synthesize import synthesize
from insightforge_agent.pipeline.write import write_report

IMPLEMENTED_SOURCE_TYPES = ["web", "documents", "memory"]


def runnable_source_types(deps: Deps) -> list[str]:
    """Documents need the chunker and the `doc_chunks` index wired in; memory needs the
    `report_sections` index."""
    wired = {"web": True, "documents": deps.doc_index is not None and deps.chunker is not None,
             "memory": deps.report_index is not None}
    return [t for t in IMPLEMENTED_SOURCE_TYPES if wired[t]]
DESCRIPTION_RULE = ('Each task description must be exactly "SUBTASK_ID=<id> <query>", with the '
                    "query copied verbatim and nothing added.")


class State(TypedDict, total=False):
    thread_id: str
    owner_id: str
    run_id: str
    brief: str
    allowed_types: list[str]
    has_uploads: bool  # documents are an allowed source type, so INGESTING runs
    documents: list[str]  # Source IDs of the ingested uploads
    watchlist_item_id: str | None  # None for a one-shot Run
    memory_reports: list[str]  # Report IDs the Memory researcher may search, fixed at submission
    plan: dict
    repaired: bool
    missing: list[str]
    research: dict
    extraction: dict
    bundle: dict
    historical: dict  # HistoricalContext: background from earlier Reports, never in `bundle`
    draft: dict
    verified: dict
    report_id: str
    question: str  # the Planner's one clarifying question, once asked
    clarification: dict  # its stored resolution: question, resolution, answer
    limitations: list[str]  # Gaps & limitations decided before writing (a timed-out question)


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


def returned_by_subtask(deps: Deps, owner_id: str, run_id: str, event_type: str,
                        key: str) -> dict[str, set[str]]:
    """Per Sub-task, the IDs (`key` of each `event_type` event) its own searches returned in
    this Run, from the Run log. A search with no Sub-task authorises nothing."""
    found: dict[str, set[str]] = {}
    cursor = 0
    while batch := deps.repos.events.read_after(owner_id, run_id, cursor):
        for e in batch:
            if e.type == event_type and e.payload.get("subtask_id"):
                found.setdefault(e.payload["subtask_id"], set()).update(e.payload[key])
        cursor = batch[-1].seq
    return found


def returned_chunks(deps: Deps, owner_id: str, run_id: str) -> dict[str, set[str]]:
    """Per Sub-task, the chunk IDs its own Doc Reader searches returned in this Run."""
    return returned_by_subtask(deps, owner_id, run_id, DOC_SEARCH_EVENT, "chunk_ids")


def returned_memory_refs(deps: Deps, owner_id: str, run_id: str) -> dict[str, set[str]]:
    """Per Sub-task, the memory references its own Memory researcher searches returned."""
    return returned_by_subtask(deps, owner_id, run_id, MEMORY_SEARCH_EVENT, "refs")


def outer_config(thread_id: str) -> dict:
    """The outer graph checkpoints under its own thread, apart from the Planner's."""
    return {"configurable": {"thread_id": f"graph-{thread_id}"}}


def run_thread_id(owner_id: str, run_id: str) -> str:
    """The Planner thread of a Run, derived from the Run so a restarted process finds its
    checkpoints (the outer graph's is `outer_config` of it)."""
    return f"thread-{owner_id}-{run_id}"


ONE_QUESTION = ("you asked a clarifying question, but this Run may not ask one (a Run asks at "
                "most one, and a Watchlist Run none); call submit_plan with a plan instead")


def clarified_prompt(clarification: dict, allowed: list[str]) -> str:
    """The PLAN message that resumes the Planner thread after its question was resolved."""
    types = f"Allowed source types: {', '.join(allowed)}."
    if clarification["resolution"] == "answered":
        return (f"PLAN (clarified). {types} You asked: {clarification['question']}\n"
                f"The user answered: {clarification['answer']}\n"
                "Call submit_plan now with a plan for the Brief and this answer. Do not ask "
                "another question.")
    return (f"PLAN (clarification timed out). {types} Nobody answered your question: "
            f"{clarification['question']}\nPlan on a best-effort reading of the Brief: call "
            "submit_plan and state the assumption you made in its `assumption` argument. Do "
            "not ask another question.")


def timeout_limitation(question: str, assumption: str) -> str:
    """The Gaps & limitations entry of a plan made after the question timed out."""
    text = (f'The clarifying question "{question}" was not answered before its deadline, so '
            "the plan rests on a best-effort reading of the Brief")
    return f"{text}: {assumption}" if assumption else f"{text}."


def build_graph(deps: Deps, interrupt_after: list[str] | None = None,
                uploads: list[Upload] | None = None):
    """Compile the outer graph for one Run, checkpointed so its state survives a restart.
    `uploads` are the Run's PDFs; their bytes stay here and never enter graph state."""
    # The Researcher tools own one Run's ids, so the Planner is built per Run, on first use.
    holder: dict = {}
    approved: dict[str, str] = {}  # Sub-task id -> query the Planner may dispatch right now
    agent_of: dict[str, str] = {}  # Sub-task id -> the subagent its source type needs

    def planner_for(state: State):
        if "planner" not in holder:
            owner, run_id = state["owner_id"], state["run_id"]
            web = make_web_search_tool(
                owner_id=owner, run_id=run_id, search=deps.search,
                fetcher=deps.fetcher, store=deps.store, summarizer=deps.light_model,
                events=deps.repos.events, now=deps.now,
                passage_token_cap=deps.passage_token_cap, lock=deps.repos.lock,
            )
            subagents = [researcher_subagent([web], deps.researcher_model)]
            if "documents" in state.get("allowed_types", []):
                docs = make_doc_search_tool(owner_id=owner, run_id=run_id, index=deps.doc_index,
                                            events=deps.repos.events, now=deps.now,
                                            lock=deps.repos.lock)
                subagents.append(doc_reader_subagent([docs], deps.researcher_model))
            if "memory" in state.get("allowed_types", []):
                memory = make_memory_search_tool(
                    owner_id=owner, run_id=run_id, index=deps.report_index,
                    report_ids=state.get("memory_reports", []), events=deps.repos.events,
                    now=deps.now, lock=deps.repos.lock)
                subagents.append(memory_researcher_subagent([memory], deps.researcher_model))
            holder["planner"] = build_planner(
                model=deps.planner_model,
                subagents=subagents,
                checkpointer=deps.checkpointer,
                guard=ApprovedDispatchGuard(
                    lambda: approved,
                    lambda sid, why, text: log_locked(owner, run_id, "dispatch_rejected",
                                                      subtask_id=sid, reason=why,
                                                      description=text),
                    agent_for=lambda sid: agent_of.get(sid, AGENT_FOR_SOURCE_TYPE["web"])),
            )
        return holder["planner"]

    def log_locked(owner: str, run_id: str, type_: str, **payload) -> None:
        """A Run-log write from a tool call, which may run beside the Researcher tools."""
        with deps.repos.lock:
            deps.log(owner, run_id, type_, **payload)

    def listing_of(state: State, ids: list[str]) -> str:
        by_id = {t["id"]: t for t in state["plan"]["sub_tasks"]}
        agent_of.update({i: AGENT_FOR_SOURCE_TYPE[by_id[i]["source_type"]] for i in ids})
        return "\n".join(f"- {i}: {by_id[i]['query']} [subagent_type {agent_of[i]}]" for i in ids)

    def cfg(state: State) -> dict:
        return {"configurable": {"thread_id": state["thread_id"]},
                "recursion_limit": deps.recursion_limit}

    def enter(state: State, stage: RunState) -> None:
        deps.repos.runs.set_state(state["owner_id"], state["run_id"], stage)
        deps.log(state["owner_id"], state["run_id"], "stage", state=stage.value)

    def thread(state: State) -> list:
        return planner_for(state).get_state(cfg(state)).values["messages"]

    def ingesting(state: State) -> State:
        enter(state, RunState.INGESTING)
        owner, run_id = state["owner_id"], state["run_id"]
        if not uploads:
            # Upload bytes are never checkpointed: a Run resumed after a restart cannot ingest.
            raise RunFailed("the uploaded documents are not available to this process; "
                            "upload them again in a new Run")
        documents = ingest_documents(deps, owner, run_id, uploads)
        allowed = state["allowed_types"]
        if not documents:
            allowed = [t for t in allowed if t != "documents"]
            deps.log(owner, run_id, "documents_unavailable",
                     reason="no uploaded document could be read")
            if not allowed:
                raise RunFailed("no uploaded document could be read and no other source type "
                                "is enabled")
        return {"documents": documents, "allowed_types": allowed}

    def planning(state: State) -> State:
        enter(state, RunState.PLANNING)
        owner, run_id = state["owner_id"], state["run_id"]
        allowed = state.get("allowed_types", IMPLEMENTED_SOURCE_TYPES)
        clarification = state.get("clarification")  # set once the one question is resolved
        thread_messages: list = []

        def attempt(prompt: str) -> tuple[SubTaskPlan | None, list[str], str | None]:
            """The plan, its violations, and the question the Planner asked instead, if any."""
            res = planner_for(state).invoke({"messages": [HumanMessage(prompt)]}, cfg(state))
            thread_messages[:] = res["messages"]
            question = asked_question(res["messages"])
            if question is not None:
                return None, [ONE_QUESTION], question
            try:
                plan = submitted_plan(res["messages"])
            except ValueError as e:
                return None, [f"planning produced no valid plan: {e}"], None
            return plan, plan_violations(plan, allowed), None

        def refuse(question: str) -> None:
            deps.log(owner, run_id, "clarification_refused", question=question[:500],
                     reason=ONE_QUESTION)

        if clarification is None:
            # Only the last 3 Run summaries, never a transcript or an earlier Report's body.
            summaries = summaries_for_planner(deps, owner, state.get("watchlist_item_id"),
                                              run_id)
            deps.log(owner, run_id, "run_summaries_loaded", count=len(summaries),
                     run_ids=[s.run_id for s in summaries])
            earlier = ("\nEarlier Runs (summaries, newest first; context for planning only):\n"
                       + render_run_summaries(summaries) if summaries else "")
            plan, problems, question = attempt(
                f"PLAN. Allowed source types: {', '.join(allowed)}. Brief: {state['brief']}"
                f"{earlier}")
            # A Vague Brief: the Planner decided it cannot plan without an answer. The graph
            # only enforces the rules: a one-shot Run, with a durable timeout to wait on.
            if question is not None:
                if state.get("watchlist_item_id") is None and deps.timeouts is not None:
                    return {"question": question[:1000]}
                refuse(question)
        else:
            plan, problems, question = attempt(clarified_prompt(clarification, allowed))
            if question is not None:
                refuse(question)
        if problems:
            too_many_aspects = plan is not None and len(plan.aspects) > MAX_ASPECTS
            deps.log(owner, run_id, "plan_correction", violations=problems)
            rules = "\n".join(f"- {p}" for p in problems)
            ask = (f"PLAN (correction). Your plan broke these rules:\n{rules}\n"
                   "Call submit_plan again with a corrected plan.")
            if too_many_aspects:
                ask += (" Consolidate genuinely related Aspects. Never drop an Aspect the Brief "
                        "asked about.")
            plan, problems, question = attempt(ask)
            if question is not None:
                refuse(question)
            if plan is not None:
                plan, trimmed = trim_plan(plan)
                if trimmed:
                    deps.log(owner, run_id, "plan_trimmed", dropped=trimmed)
                problems = plan_violations(plan, allowed)
        if plan is None or problems:
            raise RunFailed("; ".join(problems))
        deps.repos.subtasks.save_plan(owner, run_id, [
            SubTaskRecord(run_id=run_id, subtask=t) for t in plan.sub_tasks])
        limitations = []
        if clarification is not None and clarification["resolution"] == "timed_out":
            limitations.append(timeout_limitation(clarification["question"],
                                                  plan_assumption(thread_messages)[:500]))
            deps.log(owner, run_id, "plan_assumption", detail=limitations[0])
        return {"plan": plan.model_dump(), "repaired": False, "limitations": limitations}

    def clarify(state: State) -> State:
        """AWAITING_CLARIFICATION: the graph stops here, checkpointed, until `resume_run` resumes
        it with the stored resolution. Nothing before `interrupt` has a side effect, because
        LangGraph runs this node again from the top on resume."""
        return {"clarification": interrupt({"question": state["question"]})}

    def researching(state: State) -> State:
        enter(state, RunState.RESEARCHING)
        approved.clear()
        approved.update({t["id"]: t["query"] for t in state["plan"]["sub_tasks"]})
        listing = listing_of(state, list(approved))
        planner_for(state).invoke({"messages": [HumanMessage(
            f"RESEARCH: dispatch exactly these approved Sub-tasks now:\n{listing}\n{DESCRIPTION_RULE}")]}, cfg(state))
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
        listing = listing_of(state, state["missing"])
        approved.clear()
        approved.update({i: by_id[i]["query"] for i in state["missing"]})
        deps.log(state["owner_id"], state["run_id"], "repair", missing=state["missing"])
        planner_for(state).invoke({"messages": [HumanMessage(
            f"REPAIR: dispatch only these missing Sub-tasks:\n{listing}\n{DESCRIPTION_RULE}")]}, cfg(state))
        return {"repaired": True}

    def collect(state: State) -> State:
        owner, run_id = state["owner_id"], state["run_id"]
        known = {s.id for s in deps.repos.sources.list_for_run(owner, run_id)}
        summaries = summaries_from_log(deps, owner, run_id)
        returned = returned_chunks(deps, owner, run_id)
        documents = set(state.get("documents", []))
        source_type = {t["id"]: t["source_type"] for t in state["plan"]["sub_tasks"]}
        all_outcomes = outcomes(state)
        historical = memory_background(state, all_outcomes)
        found_background = {sid for sid, refs in historical.accepted.items() if refs}
        results = []
        for sid, outcome in all_outcomes.items():
            refs: list[SourceRef] = []
            if source_type[sid] == "memory":
                # Background only: no current Source, so nothing for Extraction or Synthesis.
                ok = outcome.status == "succeeded" and sid in found_background
                if not ok:
                    deps.log(owner, run_id, "subtask_failed", subtask_id=sid,
                             status=outcome.status)
                results.append(SubTaskResult(subtask_id=sid,
                                             status="succeeded" if ok else "failed"))
                deps.repos.subtasks.set_status(
                    owner, run_id, sid, SubTaskStatus.SUCCEEDED if ok else SubTaskStatus.FAILED)
                continue
            if outcome.status == "succeeded" and source_type[sid] == "documents":
                reply = outcome.dispatch.result or ""
                refs = document_refs(owner, reply, returned.get(sid, set()), documents)
                kept = {chunk_id(p) for r in refs for p in r.passages}
                rejected = [c for c in map(chunk_id, chunk_ids_in(reply)) if c not in kept]
                if rejected:
                    deps.log(owner, run_id, "document_chunks_rejected", subtask_id=sid,
                             chunk_ids=rejected[:50], count=len(rejected),
                             reason="not returned by this Sub-task's own searches in this Run")
            elif outcome.status == "succeeded":
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
        return {"research": ResearchResults(results=results).model_dump(),
                "historical": historical.context.model_dump(mode="json")}

    def memory_background(state: State, all_outcomes):
        """Historical items for the references each memory Sub-task named that its own
        searches returned, from Reports this Run may use. Anything else is rejected and logged:
        an invented reference, one returned only to another Sub-task, or one outside the
        eligible Reports (another User's, a Failed Run's, the same Watchlist Item's)."""
        owner, run_id = state["owner_id"], state["run_id"]
        plan = SubTaskPlan.model_validate(state["plan"])
        returned = returned_memory_refs(deps, owner, run_id)
        named: dict[str, list] = {}
        not_returned: dict[str, list] = {}
        for t in plan.sub_tasks:
            outcome = all_outcomes[t.id]
            if t.source_type != "memory" or outcome.status != "succeeded":
                continue
            for ref in memory_refs_in(outcome.dispatch.result or ""):
                own = memory_ref_id(ref) in returned.get(t.id, set())
                (named if own else not_returned).setdefault(t.id, []).append(ref)
        resolved = resolve_historical(deps, owner, run_id, plan, named,
                                      set(state.get("memory_reports", [])))
        for reason, rejected in (
                ("not returned by this Sub-task's own searches in this Run", not_returned),
                ("not a Claim of a Report this Run may use", resolved.rejected)):
            for sid, refs in rejected.items():
                deps.log(owner, run_id, "memory_refs_rejected", subtask_id=sid,
                         refs=[memory_ref_id(r) for r in refs][:50], count=len(refs),
                         reason=reason)
        if resolved.context.items:
            deps.log(owner, run_id, "historical_context", count=len(resolved.context.items),
                     report_ids=sorted({i.report_id for i in resolved.context.items}))
        return resolved

    def document_refs(owner: str, reply: str, returned: set[str],
                      documents: set[str]) -> list[SourceRef]:
        """The chunks a Doc Reader named that its own Sub-task's searches really returned,
        grouped by document. A chunk ID it invented, one returned only to another Sub-task, or
        one from another Run, is ignored."""
        by_source: dict[str, list] = {}
        for ref in chunk_ids_in(reply):
            if chunk_id(ref) not in returned:
                continue
            try:
                source_id = deps.repos.observations.get(owner, ref.observation_id).source_id
            except NotFoundError:
                continue
            if source_id in documents:
                by_source.setdefault(source_id, []).append(ref)
        out = []
        for source_id, chosen in by_source.items():
            source = deps.repos.sources.get(owner, source_id)
            pages = sorted({p.page for c in chosen for p in deps.store.get_passages(
                owner, c.observation_id, [c.index]) if p.page})
            out.append(SourceRef(
                source_id=source_id, title=source.title, passages=chosen,
                summary=f"{len(chosen)} chunk(s) of {source.title}"
                        + (f", pages {', '.join(map(str, pages))}" if pages else "")))
        return out

    def extracting(state: State) -> State:
        enter(state, RunState.EXTRACTING)
        research = ResearchResults.model_validate(state["research"])
        scope = research.passage_scope()
        result = extract(deps, state["owner_id"], state["run_id"], list(scope), scope)
        return {"extraction": result.model_dump()}

    def synthesizing(state: State) -> State:
        enter(state, RunState.SYNTHESIZING)
        bundle = synthesize(
            deps, state["owner_id"], state["run_id"], state["brief"],
            SubTaskPlan.model_validate(state["plan"]),
            ResearchResults.model_validate(state["research"]),
            ExtractionResult.model_validate(state["extraction"]),
        )
        if not bundle.sections and not historical_of(state).items:
            raise RunFailed("no Evidence item survived synthesis")
        return {"bundle": bundle.model_dump()}

    def writing(state: State) -> State:
        enter(state, RunState.WRITING)
        research = ResearchResults.model_validate(state["research"])
        by_id = {t.id: t for t in SubTaskPlan.model_validate(state["plan"]).sub_tasks}
        gaps = list(state.get("limitations", []))
        gaps += [f"Sub-task {r.subtask_id} ({by_id[r.subtask_id].query}) produced no usable "
                 "sources." for r in research.results if r.status == "failed"]
        bundle = EvidenceBundle.model_validate(state["bundle"])
        draft = write_report(deps, state["owner_id"], state["run_id"], state["brief"], bundle,
                             gaps, historical_of(state))
        return {"draft": draft.model_dump()}

    def fact_checking(state: State) -> State:
        enter(state, RunState.FACT_CHECKING)
        verified = fact_check(
            deps, state["owner_id"], state["run_id"], ReportDraft.model_validate(state["draft"]),
            EvidenceBundle.model_validate(state["bundle"]), historical_of(state))
        return {"verified": verified.model_dump()}

    def historical_of(state: State) -> HistoricalContext:
        return HistoricalContext.model_validate(state.get("historical") or {})

    def complete(state: State) -> State:
        owner, run_id = state["owner_id"], state["run_id"]
        draft = ReportDraft.model_validate(state["draft"])
        verified = VerifiedReport.model_validate(state["verified"])
        run = deps.repos.runs.get(owner, run_id)
        try:
            # A retry after a crash between storing the Report and marking COMPLETE reuses it.
            report = deps.repos.reports.get_for_run(owner, run_id)
        except NotFoundError:
            report = Report(
                id=derive_report_id(owner, run_id), owner_id=owner, run_id=run_id, created_at=deps.now(),
                body={"brief": state["brief"], "draft": draft.model_dump(),
                      "verified": verified.model_dump(), "evidence": state["bundle"],
                      "historical": historical_of(state).model_dump(mode="json")},
            )
            # Index and summarise first, store the Report last: a stored Report is always
            # finalised, so no COMPLETE Run (or recovered one) lacks its summary or index.
            finalise_report(deps, run, report)
            deps.repos.reports.add(report)
        else:
            if not report_finalised(deps, owner, run_id):
                finalise_report(deps, run, report)
        enter(state, RunState.COMPLETE)
        return {"report_id": report.id}

    g = StateGraph(State)
    for name, fn in [("ingesting", ingesting), ("planning", planning), ("clarify", clarify),
                     ("researching", researching), ("verify", verify),
                     ("repair", repair), ("collect", collect), ("extracting", extracting),
                     ("synthesizing", synthesizing), ("writing", writing), ("fact_checking", fact_checking),
                     ("complete", complete)]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, lambda s: "ingesting" if s.get("has_uploads") else "planning",
                            {"ingesting": "ingesting", "planning": "planning"})
    g.add_edge("ingesting", "planning")
    g.add_conditional_edges(
        "planning",
        lambda s: "clarify" if s.get("question") and not s.get("clarification") else "researching",
        {"clarify": "clarify", "researching": "researching"})
    g.add_edge("clarify", "planning")
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
    uploads: list[Upload] | None = None, watchlist_item_id: str | None = None,
) -> Run:
    """Create a Run for the Brief and carry it to COMPLETE or FAILED, or to
    AWAITING_CLARIFICATION when the Planner asks its one question (`resume_run` carries it on
    once that is resolved). A Brief with no available source type, or with uploads that break
    the document limits (at most 10 PDFs of at most 20 MB, matching `brief.document_ids`),
    raises SubmissionRejected before any Run exists. A Run that fails does not raise; its
    reason is in the Run log. `on_state` sees every graph state snapshot. `watchlist_item_id`
    marks a Watchlist Run (memory and Run summaries are then scoped to that item); None is a
    one-shot Run."""
    uploads = list(uploads or [])
    # One eligibility rule decides memory availability here and what memory may search later.
    memory_reports = eligible_report_ids(deps, owner_id, watchlist_item_id)
    available = check_submission(brief, bool(memory_reports))
    allowed = [t for t in available if t in runnable_source_types(deps)]
    if not allowed:
        raise SubmissionRejected(
            f"no runnable source type: {', '.join(available)} cannot be researched yet")
    check_uploads(brief, uploads)
    ingest = "documents" in allowed
    run = Run(id=f"run_{deps.new_id()}", owner_id=owner_id, brief=brief.text,
              state=RunState.INGESTING if ingest else RunState.PLANNING, created_at=deps.now(),
              watchlist_item_id=watchlist_item_id)
    deps.repos.runs.add(run)
    thread_id = run_thread_id(owner_id, run.id)
    initial: State = {"thread_id": thread_id, "owner_id": owner_id,
                      "run_id": run.id, "brief": brief.text,
                      "allowed_types": allowed, "has_uploads": ingest,
                      "watchlist_item_id": watchlist_item_id,
                      "memory_reports": memory_reports if "memory" in allowed else []}
    return drive(deps, owner_id, run.id, build_graph(deps, uploads=uploads if ingest else []),
                 initial, outer_config(thread_id), on_state)


def resume_run(deps: Deps, owner_id: str, run_id: str,
               on_state: Callable[[dict], None] | None = None, *, claimed: bool = False) -> Run:
    """Carry a RESUMING Run (its clarification resolved and stored) on from its checkpoint,
    giving the graph the stored resolution. Idempotent: only the caller that moves the Run from
    RESUMING to PLANNING runs it; any other call, or one for a Run in another state, changes
    nothing and returns the Run as it is. `claimed=True` skips the claim, for the startup sweep
    only: a crash came after the claim and before the graph took the resolution in
    (`awaits_resume`)."""
    if claimed:
        run = deps.repos.runs.get(owner_id, run_id)
    else:
        try:
            run = deps.repos.runs.claim_resume(owner_id, run_id)
        except ClarificationConflict:
            return deps.repos.runs.get(owner_id, run_id)
    c = run.clarification
    deps.log(owner_id, run_id, "resuming", resolution=c.resolution)
    graph = build_graph(deps)  # uploads were ingested before PLANNING; they are not needed
    config = outer_config(run_thread_id(owner_id, run_id))
    if not graph.get_state(config).interrupts:
        return settle_interrupted(deps, owner_id, run_id,
                                  "the Run's checkpoint holds no question to resume")
    return drive(deps, owner_id, run_id, graph,
                 Command(resume={"question": c.question, "resolution": c.resolution,
                                 "answer": c.answer}),
                 config, on_state)


def awaits_resume(deps: Deps, run: Run) -> bool:
    """A PLANNING Run that resolved its question but whose checkpoint still waits at it: the
    process died between `claim_resume` and the graph taking the resolution in, so nothing of
    the resumed Run has run yet and resuming it again cannot run anything twice."""
    return (run.state == RunState.PLANNING and run.clarification is not None
            and run.clarification.resolution is not None
            and bool(build_graph(deps).get_state(
                outer_config(run_thread_id(run.owner_id, run.id))).interrupts))


def drive(deps: Deps, owner_id: str, run_id: str, graph, graph_input, config: dict,
          on_state: Callable[[dict], None] | None) -> Run:
    """Run the graph until it ends, fails or stops for its clarifying question. A stop is not
    a failure: LangGraph's interrupt returns from `stream` normally with the question in the
    checkpoint, and the Run moves to AWAITING_CLARIFICATION with its timeout scheduled."""
    try:
        for snapshot in graph.stream(graph_input, config, stream_mode="values"):
            if on_state:
                on_state({k: v for k, v in snapshot.items() if k != "__interrupt__"})
    except Exception as e:  # noqa: BLE001 - any failure ends the Run, with its reason logged
        reason = str(e) if isinstance(e, RunFailed) else f"{type(e).__name__}: {e}"
        return settle_interrupted(deps, owner_id, run_id, reason)
    pending = graph.get_state(config).interrupts
    if pending:
        return await_clarification(deps, owner_id, run_id, pending[0].value["question"])
    return deps.repos.runs.get(owner_id, run_id)


def await_clarification(deps: Deps, owner_id: str, run_id: str, question: str) -> Run:
    """Store the question with its deadline (PLANNING -> AWAITING_CLARIFICATION, at most once
    per Run), then schedule the durable timeout. If the process dies between the two, the
    startup sweep schedules it again from the stored deadline."""
    asked = deps.now()
    clarification = Clarification(question=question, asked_at=asked,
                                  deadline=asked + deps.clarification_timeout)
    try:
        run = deps.repos.runs.open_clarification(owner_id, run_id, clarification)
    except ClarificationConflict as e:
        return settle_interrupted(deps, owner_id, run_id, f"cannot wait for an answer: {e}")
    deps.log(owner_id, run_id, "stage", state=RunState.AWAITING_CLARIFICATION.value)
    deps.log(owner_id, run_id, "clarification_requested", question=question,
             deadline=clarification.deadline.isoformat())
    try:
        deps.timeouts.schedule(owner_id, run_id, clarification.deadline)
    except Exception as e:  # noqa: BLE001 - the deadline is stored; the sweep recovers it
        deps.log(owner_id, run_id, "clarification_timeout_unscheduled",
                 error=f"{type(e).__name__}: {e}")
    return run


def settle_interrupted(deps: Deps, owner_id: str, run_id: str, reason: str) -> Run:
    """The final state of a Run whose graph stopped with an error. A Run that already stored its
    Report is COMPLETE, never FAILED with a Report, but only once that Report is finalised
    (summarised and indexed as memory): finalisation is retried here, and a Report that still
    cannot be finalised leaves the Run FAILED, so it never feeds memory half-finished."""
    try:
        report = deps.repos.reports.get_for_run(owner_id, run_id)
        if not report_finalised(deps, owner_id, run_id):
            finalise_report(deps, deps.repos.runs.get(owner_id, run_id), report)
    except NotFoundError:
        deps.log(owner_id, run_id, "run_failed", reason=reason)
        return deps.repos.runs.set_state(owner_id, run_id, RunState.FAILED)
    except Exception as finalising:  # noqa: BLE001 - Qdrant or the repository unavailable
        deps.log(owner_id, run_id, "run_failed", reason=f"{reason}; the Report could not be "
                 f"finalised: {type(finalising).__name__}: {finalising}")
        return deps.repos.runs.set_state(owner_id, run_id, RunState.FAILED)
    deps.log(owner_id, run_id, "completion_recovered", detail=reason)
    return deps.repos.runs.set_state(owner_id, run_id, RunState.COMPLETE)
