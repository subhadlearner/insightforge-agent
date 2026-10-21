"""The Planner: the `create_deep_agent()` root agent (ADR-0001).

It plans in PLAN, dispatches Researchers in RESEARCH and fills gaps in REPAIR, all on one
checkpointed thread. The plan is read from its `submit_plan` tool call, because
`response_format` would force a structured-output call on every resume (design.md section 12)."""

from collections.abc import Callable

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from insightforge_agent.agents.compaction import CompactionRecorder, ContextCompaction
from insightforge_agent.domain.plan import (
    PLANNER,
    RESEARCHER,
    SubTaskPlan,
    canonical_description,
    dispatched_id,
)

PLANNER_PROMPT = """You are the Planner of a research system.
Phase PLAN: the user gives a Brief and the source types you may use. Break the Brief into
Aspects (one distinct thing it asks about) and Sub-tasks, then call `submit_plan` exactly
once with a SubTaskPlan: 3 to 6 Sub-tasks, each with a unique short id (s1, s2, ...), each
covering exactly one Aspect, every Aspect covered, each with a concrete search query, a
time horizon in months and a priority (1 is most important). Use only the allowed source
types. Do NOT call `task` in this phase.
Vague Brief: if you cannot produce at least 3 Sub-tasks with a concrete scope and time
horizon from the Brief, call `ask_clarification` with ONE short question for the user instead
of `submit_plan`, then stop. A Run gets at most one question. When the user message starts
with PLAN (clarified), the user has answered: plan with the answer. When it starts with
PLAN (clarification timed out), nobody answered: plan on a best-effort reading of the Brief
and state the assumption you made in the `assumption` argument of `submit_plan`. Never ask a
second question.
Phase RESEARCH: the user message starts with RESEARCH. Call `write_todos` with one todo per
approved Sub-task, then call the `task` tool once per Sub-task with the subagent_type the
RESEARCH message names for it ("researcher" for web Sub-tasks, "doc_reader" for documents
Sub-tasks, "memory_researcher" for memory Sub-tasks). Each task description MUST be exactly
"SUBTASK_ID=<id> <query>": the id, one space, then the Sub-task query copied verbatim. Add nothing else (no full stop after the id, no
rephrasing, no extra instructions, no source type or time horizon, no reply format; the
Researcher already knows them). A description that differs by a single character is refused.
Issue all `task` calls in a single turn. Do not change the approved Sub-tasks.
Phase REPAIR: the user message starts with REPAIR and lists missing Sub-tasks. Dispatch
`task` only for those, with the same description format and subagent_type. Do not replan."""


@tool
def submit_plan(plan: SubTaskPlan, assumption: str = "") -> str:
    """Submit the Sub-task plan for the Brief. Call exactly once in the PLAN phase.
    `assumption`: only after a clarification timed out, the reading of the Brief you chose."""
    return "Plan recorded."


@tool
def ask_clarification(question: str) -> str:
    """Ask the user one clarifying question when the Brief is too vague to plan (fewer than 3
    Sub-tasks with a concrete scope and time horizon). Only once per Run, in the PLAN phase."""
    return "Question recorded. Stop now; the answer arrives in a later message."


class ApprovedDispatchGuard(AgentMiddleware):
    """Refuses a `task` call that is not for an approved Sub-task with its approved scope.

    `approved` maps Sub-task id to its query and is read at call time, so it is empty outside
    the RESEARCH and REPAIR phases and nothing can be dispatched then. A refused call never
    reaches a Researcher; the Planner gets a REJECTED reply and `on_reject` logs it. An
    accepted call is passed to `on_accept` just before its Researcher runs, so only a dispatch
    that really runs is ever reported as started."""

    def __init__(self, approved: Callable[[], dict[str, str]],
                 on_reject: Callable[[str, str, str], None],
                 agent_for: Callable[[str], str] = lambda _sid: RESEARCHER,
                 on_accept: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._approved = approved
        self._on_reject = on_reject
        self._agent_for = agent_for  # the one subagent an approved Sub-task may go to
        self._on_accept = on_accept

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] != "task":
            return handler(request)
        description = str(call["args"].get("description", ""))
        sid = dispatched_id(description)
        approved = self._approved()
        if sid is None:
            reason = "no SUBTASK_ID"
        elif sid not in approved:
            reason = f"{sid} is not an approved Sub-task"
        elif description.strip() != canonical_description(sid, approved[sid]):
            reason = f"{sid} does not carry exactly its approved scope"
        elif call["args"].get("subagent_type") != (agent := self._agent_for(sid)):
            reason = f"{sid} is not dispatched to the {agent} agent"
        else:
            if self._on_accept is not None:
                self._on_accept(sid)
            return handler(request)
        self._on_reject(sid or "", reason, description[:500])
        return ToolMessage(
            content=f"REJECTED: {reason}. Dispatch only the approved Sub-tasks, unchanged.",
            tool_call_id=call["id"], status="error")


def build_planner(*, model: BaseChatModel, subagents: list[dict], checkpointer,
                  guard: ApprovedDispatchGuard,
                  on_compaction: Callable[[ContextCompaction], None] | None = None):
    """`on_compaction` receives every context compaction of the Planner's thread and of each
    subagent dispatch, attributed to the agent that compacted."""
    recorders = []
    if on_compaction is not None:
        recorders = [CompactionRecorder(PLANNER, on_compaction)]
        subagents = [{**s, "middleware": [*s.get("middleware", []),
                                          CompactionRecorder(s["name"], on_compaction)]}
                     for s in subagents]
    return create_deep_agent(
        model=model,
        system_prompt=PLANNER_PROMPT,
        subagents=subagents,
        tools=[submit_plan, ask_clarification],
        middleware=[TodoListMiddleware(), guard, *recorders],
        checkpointer=checkpointer,
    )


def submitted_plan(messages) -> SubTaskPlan:
    """The plan from the latest `submit_plan` call. Raises ValueError if there is none, or
    if its arguments do not form a SubTaskPlan."""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                if c["name"] == "submit_plan":
                    return SubTaskPlan.model_validate(c["args"]["plan"])
    raise ValueError("the Planner never called submit_plan")


def _latest_call(messages, name: str) -> dict | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                if c["name"] == name:
                    return c
    return None


def plan_assumption(messages) -> str:
    """The assumption the latest `submit_plan` call stated, or ""."""
    found = _latest_call(messages, "submit_plan")
    return str(found["args"].get("assumption") or "").strip() if found else ""


def asked_question(messages) -> str | None:
    """The question the Planner asked in its latest turn (after the last user message), when
    it asked one and submitted no plan in that turn."""
    turn = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        turn.append(m)
    turn.reverse()
    if _latest_call(turn, "submit_plan") is not None:
        return None  # a plan takes precedence over a question in the same turn
    found = _latest_call(turn, "ask_clarification")
    question = str(found["args"].get("question") or "").strip() if found else ""
    return question or None
