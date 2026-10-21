"""The T1 spike graph run end to end with scripted models: no API calls.

Covers the review points: the repair result is fresh and from the repair dispatch,
initial dispatch is one-to-one with the approved plan and scope, RESEARCHING relies on
the thread (not the prompt) for the plan, and Researchers are isolated.
"""

import re

from langchain_core.messages import AIMessage

from tests.live.planner_spike import run_spike
from insightforge_agent.pipeline.dispatch import check_dispatches
from tests.scripted import ScriptedChat, ai, call

BRIEF_MARKER = "ZEBRA-BRIEF-MARKER compare three carmakers"
QUERIES = {
    "s1": "QUERY-ALPHA sales volumes 2025",
    "s2": "QUERY-BRAVO pricing strategy",
    "s3": "QUERY-CHARLIE battery technology",
}
PLAN_ARGS = {"plan": {
    "aspects": [{"id": "a1", "name": "Sales"}, {"id": "a2", "name": "Pricing"},
                {"id": "a3", "name": "Batteries"}],
    "sub_tasks": [
        {"id": sid, "aspect_id": f"a{n}", "query": q, "source_type": "web",
         "time_horizon_months": 12, "priority": n}
        for n, (sid, q) in enumerate(QUERIES.items(), start=1)
    ],
}}


def task(sid: str, call_id: str) -> dict:
    return call("task", {"description": f"SUBTASK_ID={sid} {QUERIES[sid]}",
                         "subagent_type": "researcher"}, call_id)


def planner(research_ids: list[str], repair_ids: list[str]) -> ScriptedChat:
    todos = call("write_todos", {"todos": [
        {"content": q, "status": "pending"} for q in QUERIES.values()]}, "todo-1")
    script = [
        ai(call("submit_plan", PLAN_ARGS, "plan-1")),
        ai(text="Plan submitted."),
        ai(todos, *[task(s, f"research-{s}") for s in research_ids]),
        ai(text="Dispatched."),
    ]
    script += [ai(*[task(s, f"repair-{s}") for s in repair_ids]), ai(text="Repair done.")]
    return ScriptedChat(script=script)


def researcher(fail_ids: tuple[str, ...] = ()) -> ScriptedChat:
    def reply(messages) -> AIMessage:
        text = " ".join(str(m.content) for m in messages)
        failing = any(f"SUBTASK_ID={sid}" in text for sid in fail_ids)
        return AIMessage(content="FAILED: no data" if failing else "RESULT: found it")
    return ScriptedChat(reply=reply)


def run(research_ids, repair_ids, *, simulate_drop, res=None):
    res = res or researcher()
    return run_spike(planner_model=planner(research_ids, repair_ids), researcher_model=res,
                     brief=BRIEF_MARKER, simulate_drop=simulate_drop), res


def test_genuinely_missing_subtask_gets_fresh_result_from_repair_dispatch():
    out, _ = run(["s1", "s2"], ["s3"], simulate_drop=False)
    # Initial dispatch: one-to-one with the approved plan (s3 truly omitted), scope intact.
    check_dispatches(out.plan, out.research, ["s1", "s2"])
    assert {i for i, o in out.before_repair.items() if o.status == "missing"} == {"s3"}
    # Repair dispatches only the missing ID, with its original scope.
    check_dispatches(out.plan, out.repair, ["s3"])
    fixed = out.after_repair["s3"]
    assert (fixed.status, fixed.phase) == ("succeeded", "REPAIR")
    assert fixed.dispatch.tool_call_id == "repair-s3"
    assert "repair-s3" not in {d.tool_call_id for d in out.research}
    # Successful results are untouched by the repair round.
    for sid in ("s1", "s2"):
        assert out.after_repair[sid].dispatch.tool_call_id == f"research-{sid}"
        assert out.after_repair[sid].phase == "RESEARCH"


def test_original_result_in_history_is_not_credited_when_repair_does_nothing():
    # Every Sub-task is dispatched and answered in RESEARCH, s3's result is treated as
    # never arrived, and the repair turn dispatches nothing. A verifier that read the
    # whole thread would wrongly report s3 as done.
    out, _ = run(["s1", "s2", "s3"], [], simulate_drop=True)
    assert out.repair == []
    assert out.after_repair["s3"].status == "missing"
    assert out.after_repair["s3"].dispatch is None


def test_repair_result_is_credited_to_repair_dispatch_not_the_original():
    out, _ = run(["s1", "s2", "s3"], ["s3"], simulate_drop=True)
    check_dispatches(out.plan, out.research, ["s1", "s2", "s3"])
    check_dispatches(out.plan, out.repair, ["s3"])
    fixed = out.after_repair["s3"]
    assert (fixed.phase, fixed.dispatch.tool_call_id) == ("REPAIR", "repair-s3")
    assert fixed.dispatch.tool_call_id != out.before_repair["s1"].dispatch.tool_call_id


def test_explicit_failure_in_repair_is_failed_not_missing():
    out, _ = run(["s1", "s2"], ["s3"], simulate_drop=False, res=researcher(fail_ids=("s3",)))
    assert out.after_repair["s3"].status == "failed"
    assert out.after_repair["s3"].phase == "REPAIR"


def test_researching_uses_the_approved_plan_from_the_thread_not_the_prompt():
    out, _ = run(["s1", "s2", "s3"], [], simulate_drop=False)
    assert out.thread_plan == out.plan  # what the graph holds is what the thread holds
    for q in QUERIES.values():
        assert q not in out.research_prompt  # RESEARCH message carries no plan text
    check_dispatches(out.plan, out.research, ["s1", "s2", "s3"])  # yet the scopes match


def test_one_task_per_subtask_in_a_single_turn_is_recorded():
    out, _ = run(["s1", "s2", "s3"], [], simulate_drop=False)
    assert out.research_turn_task_counts == [3]


def test_researcher_cannot_see_brief_or_other_subtasks():
    out, res = run(["s1", "s2", "s3"], [], simulate_drop=False)
    assert len(res.seen) == 3
    for conversation in res.seen:
        text = "\n".join(str(m.content) for m in conversation)
        ids = set(re.findall(r"SUBTASK_ID=(\w+)", text))
        assert len(ids) == 1, f"researcher saw several Sub-tasks: {ids}"
        (own,) = ids
        assert QUERIES[own] in text
        assert BRIEF_MARKER not in text
        for other, q in QUERIES.items():
            if other != own:
                assert q not in text
        assert "submit_plan" not in text and "Aspect" not in text
