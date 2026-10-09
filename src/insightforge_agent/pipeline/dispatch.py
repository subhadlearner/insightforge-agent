"""Pure checks over a Planner thread's messages: dispatch matching and repair (ADR-0003).

No I/O and no model calls, so every rule here is covered by deterministic tests.
"""

import re
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from insightforge_agent.domain.plan import SubTaskPlan

RESULT_PREFIX = "RESULT:"
FAILED_PREFIX = "FAILED:"
PHASES = ("PLAN", "RESEARCH", "REPAIR")

Status = Literal["succeeded", "failed", "missing"]


@dataclass(frozen=True)
class Dispatch:
    """One `task` call and the ToolMessage that answered it, if any."""

    tool_call_id: str
    subtask_id: str
    description: str
    result: str | None

    @property
    def status(self) -> Status:
        """`failed` only when the Researcher said so explicitly. A reply that is
        neither a RESULT nor a FAILED, or no reply at all, counts as missing."""
        text = (self.result or "").lstrip()
        if text.startswith(RESULT_PREFIX):
            return "succeeded"
        if text.startswith(FAILED_PREFIX):
            return "failed"
        return "missing"


@dataclass(frozen=True)
class Outcome:
    """The authoritative result for one Sub-task ID and where it came from."""

    status: Status
    dispatch: Dispatch | None
    phase: str | None


def segments(messages) -> dict[str, list]:
    """Split a thread by the phase marker at the start of each human message."""
    out: dict[str, list] = {}
    current: list | None = None
    for m in messages:
        if isinstance(m, HumanMessage):
            text = str(m.content)
            phase = next((p for p in PHASES if text.startswith(p)), None)
            if phase:
                current = out.setdefault(phase, [])
        if current is not None:
            current.append(m)
    return out


def dispatches(messages) -> list[Dispatch]:
    """Every `task` call carrying a SUBTASK_ID, paired with its ToolMessage."""
    replies = {m.tool_call_id: str(m.content) for m in messages if isinstance(m, ToolMessage)}
    out = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for call in m.tool_calls:
            if call["name"] != "task":
                continue
            description = str(call["args"].get("description", ""))
            found = re.search(r"SUBTASK_ID=(\w+)", description)
            if found:
                out.append(Dispatch(call["id"], found.group(1), description, replies.get(call["id"])))
    return out


def tasks_per_turn(messages) -> list[int]:
    return [
        n for m in messages if isinstance(m, AIMessage)
        if (n := sum(1 for c in m.tool_calls if c["name"] == "task"))
    ]


def _authoritative(candidates: list[Dispatch]) -> Dispatch | None:
    """One result per ID: the first success, else the first explicit failure.
    A late or duplicate result never overwrites a success."""
    for wanted in ("succeeded", "failed"):
        for d in candidates:
            if d.status == wanted:
                return d
    return None


def resolve(
    plan_ids: list[str],
    research: list[Dispatch],
    repair: list[Dispatch],
    discard: frozenset[str] = frozenset(),
) -> dict[str, Outcome]:
    """Authoritative outcome per Sub-task ID.

    A repair result only fills an ID that has no usable research result. An explicit
    failure is final: it is never retried. `discard` simulates research results that
    never arrived (the research dispatch is ignored)."""
    out: dict[str, Outcome] = {}
    for sid in plan_ids:
        base = None if sid in discard else _authoritative([d for d in research if d.subtask_id == sid])
        if base:
            out[sid] = Outcome(base.status, base, "RESEARCH")
            continue
        fixed = _authoritative([d for d in repair if d.subtask_id == sid])
        out[sid] = Outcome(fixed.status, fixed, "REPAIR") if fixed else Outcome("missing", None, None)
    return out


def missing_ids(outcomes: dict[str, Outcome]) -> list[str]:
    return [sid for sid, o in outcomes.items() if o.status == "missing"]


def check_dispatches(plan: SubTaskPlan, ds: list[Dispatch], expected_ids: list[str]) -> None:
    """Exactly one dispatch per expected ID, none invented or duplicated, and each
    carrying the approved scope (the Sub-task's own query) verbatim."""
    by_id = {t.id: t for t in plan.sub_tasks}
    got = sorted(d.subtask_id for d in ds)
    assert got == sorted(expected_ids), f"dispatched {got}, expected {sorted(expected_ids)}"
    for d in ds:
        query = by_id[d.subtask_id].query
        assert query in d.description, (
            f"{d.subtask_id}: dispatch lost the approved scope {query!r}: {d.description!r}"
        )
