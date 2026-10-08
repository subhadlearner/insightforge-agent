"""Spike harness: one checkpointed Planner thread resumed across outer graph nodes.

See docs/design.md section 12 and ADR-0001 / ADR-0003. Uses a real model and a
fake researcher tool. Not imported by the default test run.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import TypedDict

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from insightforge_agent.config import Settings
from insightforge_agent.domain.plan import SubTaskPlan
from insightforge_agent.llm import build_chat_model

BRIEF = (
    "Compare the 2025 market position of Tesla, BYD and Volkswagen in battery "
    "electric vehicles: sales volume, pricing strategy and battery technology."
)

PLANNER_PROMPT = """You are the Planner of a research system.
Phase PLAN: the user gives a Brief. Call `submit_plan` exactly once with a
SubTaskPlan: 3 to 6 Sub-tasks, each with a unique short id (s1, s2, ...), covering
exactly one Aspect, all source_type "web". Do NOT call `task` in this phase.
Phase RESEARCH: the user message starts with RESEARCH. Call `write_todos` with
one todo per approved Sub-task, then call the `task` tool once per Sub-task with
subagent_type "researcher". Each task description MUST begin with
"SUBTASK_ID=<id>" followed by the Sub-task query. Issue all `task` calls in a
single turn. Do not change the approved Sub-tasks.
Phase REPAIR: the user message starts with REPAIR and lists missing Sub-tasks.
Dispatch `task` only for those, with the same description format. Do not replan."""

RESEARCHER_PROMPT = (
    "You are a Researcher. Call `lookup` once with your task's query, then reply "
    "with exactly 'RESULT: ' followed by a one-line summary of what lookup returned."
)


@tool
def lookup(query: str) -> str:
    """Look up notes for a research query."""
    return f"Canned notes about: {query}"


@tool
def submit_plan(plan: SubTaskPlan) -> str:
    """Submit the Sub-task plan for the Brief. Call exactly once in the PLAN phase."""
    return "Plan recorded."


def _submitted_plan(messages) -> SubTaskPlan:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                if c["name"] == "submit_plan":
                    return SubTaskPlan.model_validate(c["args"]["plan"])
    raise AssertionError("Planner never called submit_plan")


class State(TypedDict, total=False):
    thread_id: str
    plan: dict
    results: dict[str, str]
    missing: list[str]
    repaired: bool
    drop_id: str


@dataclass
class SpikeOutcome:
    plan: SubTaskPlan | None = None
    missing_after_research: list[str] = field(default_factory=list)
    final_results: dict[str, str] = field(default_factory=dict)
    research_turn_task_counts: list[int] = field(default_factory=list)
    plan_survived_resume: bool = False


def _task_calls(messages) -> dict[str, str]:
    """tool_call_id -> sub-task id, for `task` calls in the given messages."""
    out = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                if c["name"] == "task":
                    found = re.search(r"SUBTASK_ID=(\w+)", str(c["args"].get("description", "")))
                    if found:
                        out[c["id"]] = found.group(1)
    return out


def _collect(messages) -> dict[str, str]:
    calls = _task_calls(messages)
    results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage) and m.tool_call_id in calls and "RESULT:" in str(m.content):
            results.setdefault(calls[m.tool_call_id], str(m.content))
    return results


def _turn_counts(messages) -> list[int]:
    return [
        n for m in messages if isinstance(m, AIMessage)
        if (n := sum(1 for c in m.tool_calls if c["name"] == "task"))
    ]


def run_spike(settings: Settings | None = None) -> SpikeOutcome:
    s = settings or Settings()
    outcome = SpikeOutcome()
    with SqliteSaver.from_conn_string(":memory:") as saver:
        planner = create_deep_agent(
            model=build_chat_model("planner", s),
            system_prompt=PLANNER_PROMPT,
            subagents=[{
                "name": "researcher",
                "description": "Researches one Sub-task and returns RESULT: <summary>.",
                "system_prompt": RESEARCHER_PROMPT,
                "tools": [lookup],
                "model": build_chat_model("light", s),
            }],
            tools=[submit_plan],
            middleware=[TodoListMiddleware()],
            checkpointer=saver,
        )

        def cfg(state: State) -> dict:
            return {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 60}

        def planning(state: State) -> State:
            res = planner.invoke(
                {"messages": [HumanMessage(f"PLAN. Brief: {BRIEF}")]}, cfg(state)
            )
            plan = _submitted_plan(res["messages"])
            return {"plan": plan.model_dump(), "repaired": False,
                    "drop_id": plan.sub_tasks[-1].id}

        def researching(state: State) -> State:
            planner.invoke(
                {"messages": [HumanMessage("RESEARCH: dispatch the approved Sub-tasks now.")]},
                cfg(state),
            )
            return {}

        def verify(state: State) -> State:
            results = _collect(planner.get_state(cfg(state)).values["messages"])
            if not state["repaired"]:
                # Deliberately drop one Sub-task's result to exercise the repair round.
                results.pop(state["drop_id"], None)
            ids = [t["id"] for t in state["plan"]["sub_tasks"]]
            return {"results": results, "missing": [i for i in ids if i not in results]}

        def repair(state: State) -> State:
            by_id = {t["id"]: t for t in state["plan"]["sub_tasks"]}
            listing = "\n".join(f"- {i}: {by_id[i]['query']}" for i in state["missing"])
            planner.invoke(
                {"messages": [HumanMessage(f"REPAIR: dispatch only these missing Sub-tasks:\n{listing}")]},
                cfg(state),
            )
            return {"repaired": True}

        g = StateGraph(State)
        g.add_node("planning", planning)
        g.add_node("researching", researching)
        g.add_node("verify", verify)
        g.add_node("repair", repair)
        g.add_edge(START, "planning")
        g.add_edge("planning", "researching")
        g.add_edge("researching", "verify")
        g.add_conditional_edges(
            "verify",
            lambda st: "repair" if st["missing"] and not st["repaired"] else END,
            {"repair": "repair", END: END},
        )
        g.add_edge("repair", "verify")
        graph = g.compile()

        thread_id = f"spike-{uuid.uuid4()}"
        first_verify = True
        final: State = {}
        for update in graph.stream({"thread_id": thread_id}, stream_mode="updates"):
            for node, delta in update.items():
                if node == "planning":
                    outcome.plan = SubTaskPlan.model_validate(delta["plan"])
                if node == "verify":
                    if first_verify:
                        outcome.missing_after_research = list(delta["missing"])
                        first_verify = False
                    final = delta

        outcome.final_results = final.get("results", {})
        msgs = planner.get_state({"configurable": {"thread_id": thread_id}}).values["messages"]
        human = [i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)]
        outcome.plan_survived_resume = len(human) >= 2 and "PLAN." in str(msgs[human[0]].content)
        research_start = next(i for i in human if str(msgs[i].content).startswith("RESEARCH"))
        repair_start = next(
            (i for i in human if str(msgs[i].content).startswith("REPAIR")), len(msgs)
        )
        outcome.research_turn_task_counts = _turn_counts(msgs[research_start:repair_start])
    return outcome
