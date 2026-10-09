"""The Planner: the `create_deep_agent()` root agent (ADR-0001).

It plans in PLAN, dispatches Researchers in RESEARCH and fills gaps in REPAIR, all on one
checkpointed thread. The plan is read from its `submit_plan` tool call, because
`response_format` would force a structured-output call on every resume (design.md section 12)."""

from collections.abc import Callable

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from insightforge_agent.domain.plan import (
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
Phase RESEARCH: the user message starts with RESEARCH. Call `write_todos` with one todo per
approved Sub-task, then call the `task` tool once per Sub-task with subagent_type
"researcher". Each task description MUST begin with "SUBTASK_ID=<id>" followed by the
Sub-task query. Issue all `task` calls in a single turn. Do not change the approved Sub-tasks.
Phase REPAIR: the user message starts with REPAIR and lists missing Sub-tasks. Dispatch
`task` only for those, with the same description format. Do not replan."""


@tool
def submit_plan(plan: SubTaskPlan) -> str:
    """Submit the Sub-task plan for the Brief. Call exactly once in the PLAN phase."""
    return "Plan recorded."


class ApprovedDispatchGuard(AgentMiddleware):
    """Refuses a `task` call that is not for an approved Sub-task with its approved scope.

    `approved` maps Sub-task id to its query and is read at call time, so it is empty outside
    the RESEARCH and REPAIR phases and nothing can be dispatched then. A refused call never
    reaches a Researcher; the Planner gets a REJECTED reply and `on_reject` logs it."""

    def __init__(self, approved: Callable[[], dict[str, str]],
                 on_reject: Callable[[str, str], None]) -> None:
        super().__init__()
        self._approved = approved
        self._on_reject = on_reject

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
        elif call["args"].get("subagent_type") != RESEARCHER:
            reason = f"{sid} is not dispatched to the {RESEARCHER} agent"
        else:
            return handler(request)
        self._on_reject(sid or "", reason)
        return ToolMessage(
            content=f"REJECTED: {reason}. Dispatch only the approved Sub-tasks, unchanged.",
            tool_call_id=call["id"], status="error")


def build_planner(*, model: BaseChatModel, subagents: list[dict], checkpointer,
                  guard: ApprovedDispatchGuard):
    return create_deep_agent(
        model=model,
        system_prompt=PLANNER_PROMPT,
        subagents=subagents,
        tools=[submit_plan],
        middleware=[TodoListMiddleware(), guard],
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



