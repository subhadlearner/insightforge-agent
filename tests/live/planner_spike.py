"""Spike harness: one checkpointed Planner thread resumed across outer graph nodes.

See docs/design.md section 12 and ADR-0001 / ADR-0003. Models are injectable, so the
same graph runs against real models (live tests) or scripted ones (default tests).
"""

import uuid
from dataclasses import dataclass, field
from typing import TypedDict

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from insightforge_agent.config import Settings
from insightforge_agent.domain.plan import SubTaskPlan
from insightforge_agent.llm import build_chat_model
from tests.live.spike_checks import (
    Dispatch,
    Outcome,
    dispatches,
    missing_ids,
    resolve,
    segments,
    tasks_per_turn,
)

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
    "with exactly 'RESULT: ' followed by a one-line summary of what lookup returned. "
    "If you cannot, reply with 'FAILED: ' and the reason."
)


@tool
def lookup(query: str) -> str:
    """Look up notes for a research query."""
    return f"Canned notes about: {query}"


@tool
def submit_plan(plan: SubTaskPlan) -> str:
    """Submit the Sub-task plan for the Brief. Call exactly once in the PLAN phase."""
    return "Plan recorded."


def submitted_plan(messages) -> SubTaskPlan:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            for c in m.tool_calls:
                if c["name"] == "submit_plan":
                    return SubTaskPlan.model_validate(c["args"]["plan"])
    raise AssertionError("Planner never called submit_plan")


class State(TypedDict, total=False):
    thread_id: str
    plan: dict
    missing: list[str]
    repaired: bool
    discard: list[str]


@dataclass
class SpikeOutcome:
    """Everything the tests assert on, recomputed from the final thread."""

    plan: SubTaskPlan | None = None
    thread_plan: SubTaskPlan | None = None  # the plan as submitted in the thread
    research_prompt: str = ""  # the RESEARCH message text, to show it carries no plan
    research: list[Dispatch] = field(default_factory=list)
    repair: list[Dispatch] = field(default_factory=list)
    research_turn_task_counts: list[int] = field(default_factory=list)
    before_repair: dict[str, Outcome] = field(default_factory=dict)
    after_repair: dict[str, Outcome] = field(default_factory=dict)
    discarded: frozenset[str] = frozenset()


def _spike_model(role: str, s: Settings) -> BaseChatModel:
    model = build_chat_model(role, s)  # type: ignore[arg-type]
    if s.llm_provider == "anthropic":
        # Thinking blocks are signed against the exact message prefix; deepagents
        # rewrites earlier messages on resume, which the API then rejects.
        model.thinking = {"type": "disabled"}  # type: ignore[attr-defined]
    return model


def run_spike(
    settings: Settings | None = None,
    *,
    planner_model: BaseChatModel | None = None,
    researcher_model: BaseChatModel | None = None,
    brief: str = BRIEF,
    simulate_drop: bool = True,
) -> SpikeOutcome:
    """Run PLANNING -> RESEARCHING -> verify -> (repair) on one checkpointed thread.

    With `simulate_drop`, the verifier ignores the last Sub-task's research result, as
    if it never arrived. Scripted tests instead make the Planner genuinely omit one."""
    if planner_model is None or researcher_model is None:
        s = settings or Settings()
        planner_model = planner_model or _spike_model("planner", s)
        researcher_model = researcher_model or _spike_model("light", s)

    with SqliteSaver.from_conn_string(":memory:") as saver:
        planner = create_deep_agent(
            model=planner_model,
            system_prompt=PLANNER_PROMPT,
            subagents=[{
                "name": "researcher",
                "description": "Researches one Sub-task and returns RESULT: <summary>.",
                "system_prompt": RESEARCHER_PROMPT,
                "tools": [lookup],
                "model": researcher_model,
            }],
            tools=[submit_plan],
            middleware=[TodoListMiddleware()],
            checkpointer=saver,
        )

        def cfg(state: State) -> dict:
            return {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 60}

        def thread(state: State) -> list:
            return planner.get_state(cfg(state)).values["messages"]

        def outcomes(state: State, *, with_repair: bool) -> dict[str, Outcome]:
            segs = segments(thread(state))
            ids = [t["id"] for t in state["plan"]["sub_tasks"]]
            return resolve(
                ids,
                dispatches(segs.get("RESEARCH", [])),
                dispatches(segs.get("REPAIR", [])) if with_repair else [],
                frozenset(state["discard"]),
            )

        def planning(state: State) -> State:
            res = planner.invoke({"messages": [HumanMessage(f"PLAN. Brief: {brief}")]}, cfg(state))
            plan = submitted_plan(res["messages"])
            discard = [plan.sub_tasks[-1].id] if simulate_drop else []
            return {"plan": plan.model_dump(), "repaired": False, "discard": discard}

        def researching(state: State) -> State:
            planner.invoke(
                {"messages": [HumanMessage("RESEARCH: dispatch the approved Sub-tasks now.")]},
                cfg(state),
            )
            return {}

        def verify(state: State) -> State:
            return {"missing": missing_ids(outcomes(state, with_repair=state["repaired"]))}

        def repair(state: State) -> State:
            by_id = {t["id"]: t for t in state["plan"]["sub_tasks"]}
            listing = "\n".join(f"- {i}: {by_id[i]['query']}" for i in state["missing"])
            planner.invoke(
                {"messages": [HumanMessage(
                    f"REPAIR: dispatch only these missing Sub-tasks:\n{listing}"
                )]},
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
        final = graph.invoke({"thread_id": thread_id})

        msgs = thread(final)
        segs = segments(msgs)
        plan = SubTaskPlan.model_validate(final["plan"])
        research = dispatches(segs.get("RESEARCH", []))
        repair_ds = dispatches(segs.get("REPAIR", []))
        ids = [t.id for t in plan.sub_tasks]
        discard = frozenset(final["discard"])
        return SpikeOutcome(
            plan=plan,
            thread_plan=submitted_plan(msgs),
            research_prompt=str(segs["RESEARCH"][0].content),
            research=research,
            repair=repair_ds,
            research_turn_task_counts=tasks_per_turn(segs.get("RESEARCH", [])),
            before_repair=resolve(ids, research, [], discard),
            after_repair=resolve(ids, research, repair_ds, discard),
            discarded=discard,
        )
