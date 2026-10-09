"""The Planner: the `create_deep_agent()` root agent (ADR-0001).

It plans in PLAN, dispatches Researchers in RESEARCH and fills gaps in REPAIR, all on one
checkpointed thread. The plan is read from its `submit_plan` tool call, because
`response_format` would force a structured-output call on every resume (design.md section 12)."""

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from insightforge_agent.domain.plan import SubTaskPlan

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


def build_planner(*, model: BaseChatModel, subagents: list[dict], checkpointer):
    return create_deep_agent(
        model=model,
        system_prompt=PLANNER_PROMPT,
        subagents=subagents,
        tools=[submit_plan],
        middleware=[TodoListMiddleware()],
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



