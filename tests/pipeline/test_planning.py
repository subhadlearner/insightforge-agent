"""T4: Planner bounds, Aspect coverage, trimming, and the Submission check."""

import copy

import pytest

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.models import RunState, SubTaskStatus
from insightforge_agent.domain.plan import SubTaskPlan, plan_violations, trim_plan
from insightforge_agent.domain.submission import check_submission
from insightforge_agent.pipeline.graph import run_brief
from tests.pipeline import fakes
from tests.pipeline.test_happy_path import BRIEF, events
from tests.scripted import ScriptedChat, ai, call

WEB = ["web"]


def plan_of(specs, aspects=None, source_type="web") -> SubTaskPlan:
    """specs: (subtask id, aspect id, priority). Aspects default to those the specs mention."""
    names = aspects or sorted({a for _, a, _ in specs})
    return SubTaskPlan.model_validate({
        "aspects": [{"id": a, "name": a.upper()} for a in names],
        "sub_tasks": [{"id": i, "aspect_id": a, "query": f"q {i}", "source_type": source_type,
                       "time_horizon_months": 12, "priority": p} for i, a, p in specs]})


THREE = [("s1", "a", 1), ("s2", "b", 2), ("s3", "c", 3)]


# --- pure rules ---------------------------------------------------------------------------

def test_a_plan_inside_the_bounds_has_no_violations():
    assert plan_violations(plan_of(THREE), WEB) == []


def test_an_aspect_may_have_several_subtasks():
    assert plan_violations(plan_of([("s1", "a", 1), ("s2", "a", 2), ("s3", "b", 3)]), WEB) == []


def test_too_few_and_too_many_subtasks_are_named():
    (few,) = plan_violations(plan_of([("s1", "a", 1), ("s2", "b", 2)]), WEB)
    assert "needs at least 3" in few
    (many,) = plan_violations(plan_of([(f"s{n}", "a", n + 1) for n in range(7)]), WEB)
    assert "at most 6" in many


def test_an_uncovered_aspect_is_named():
    plan = plan_of([("s1", "a", 1), ("s2", "a", 2), ("s3", "b", 3)], aspects=["a", "b", "c"])
    assert plan_violations(plan, WEB) == ["no Sub-task covers these Aspects: C"]


def test_a_source_type_that_is_not_allowed_is_named():
    (v,) = plan_violations(plan_of(THREE, source_type="documents"), WEB)
    assert "not allowed: documents" in v


def test_more_than_six_aspects_is_a_violation():
    plan = plan_of([(f"s{n}", f"a{n}", n + 1) for n in range(7)])
    assert any("7 Aspects" in v for v in plan_violations(plan, WEB))


def test_trim_drops_lowest_priority_first_and_keeps_every_aspect():
    plan = plan_of([("s1", "a", 1), ("s2", "a", 5), ("s3", "b", 2), ("s4", "b", 4),
                    ("s5", "c", 3), ("s6", "d", 9), ("s7", "e", 8), ("s8", "e", 6)])
    trimmed, dropped = trim_plan(plan)
    assert dropped == ["s7", "s2"]  # s6 (9) ranks lowest but is alone in its Aspect
    assert {t.aspect_id for t in trimmed.sub_tasks} == {"a", "b", "c", "d", "e"}
    assert len(trimmed.sub_tasks) == 6


def test_trim_breaks_ties_by_dropping_the_later_subtask():
    plan = plan_of([("s1", "a", 1), ("s2", "a", 2), ("s3", "a", 2), ("s4", "b", 1),
                    ("s5", "c", 1), ("s6", "d", 1), ("s7", "e", 1)])
    assert trim_plan(plan)[1] == ["s3"]


def test_trim_cannot_help_when_every_aspect_needs_its_only_subtask():
    plan = plan_of([(f"s{n}", f"a{n}", n + 1) for n in range(7)])
    assert trim_plan(plan) == (plan, [])


def test_the_submission_check_accepts_web():
    assert check_submission(Brief(text="x"), False) == ["web"]


@pytest.mark.parametrize("toggles, ids, reports", [
    ({"documents": True}, [], False),  # docs-only, nothing uploaded
    ({"memory": True}, [], False),  # memory-only, no eligible Report
    ({"web": False, "documents": False, "memory": False}, [], True),  # all toggles off
    ({}, ["d1"], True),  # nothing enabled
])
def test_the_submission_check_rejects_a_brief_with_no_available_source(toggles, ids, reports):
    with pytest.raises(SubmissionRejected):
        check_submission(Brief(text="x", source_toggles=toggles, document_ids=ids), reports)


def test_web_with_unavailable_documents_proceeds_on_web():
    brief = Brief(text="x", source_toggles={"web": True, "documents": True})
    assert check_submission(brief, False) == ["web"]


def test_documents_and_memory_are_available_when_backed():
    brief = Brief(text="x", source_toggles={"documents": True, "memory": True}, document_ids=["d"])
    assert check_submission(brief, True) == ["documents", "memory"]


# --- through the graph --------------------------------------------------------------------

def planner_script(*plans, dispatch=None) -> ScriptedChat:
    """One submit_plan turn per plan given, then the research dispatch of `dispatch`
    (Sub-task id -> description text), by default the three standard Sub-tasks."""
    dispatch = dispatch or fakes.QUERIES
    script = []
    for n, args in enumerate(plans):
        script += [ai(call("submit_plan", args, f"plan-{n}")), ai(text="Plan submitted.")]
    todos = call("write_todos", {"todos": [
        {"content": q, "status": "pending"} for q in dispatch.values()]}, "todo-1")
    tasks = [call("task", {"description": f"SUBTASK_ID={sid} {text}",
                           "subagent_type": "researcher"}, f"research-{sid}")
             for sid, text in dispatch.items()]
    return ScriptedChat(script=script + [ai(todos, *tasks), ai(text="Dispatched.")])


def two_subtasks() -> dict:
    args = copy.deepcopy(fakes.PLAN_ARGS)
    args["plan"]["sub_tasks"] = args["plan"]["sub_tasks"][:2]
    return args


def seven_aspects() -> dict:
    return {"plan": {
        "aspects": [{"id": f"a{n}", "name": f"Aspect {n}"} for n in range(7)],
        "sub_tasks": [{"id": f"s{n}", "aspect_id": f"a{n}", "query": fakes.QUERIES["s1"],
                       "source_type": "web", "time_horizon_months": 12, "priority": n + 1}
                      for n in range(7)]}}


def reprompts(model) -> list[str]:
    return sorted({str(m.content) for conv in model.seen for m in conv
                   if m.type == "human" and str(m.content).startswith("PLAN (correction)")})


def failure_reason(deps, run) -> str:
    (failed,) = [e for e in events(deps, run) if e.type == "run_failed"]
    return failed.payload["reason"]


def test_a_plan_outside_the_bounds_gets_exactly_one_corrective_reprompt(make_deps):
    model = planner_script(two_subtasks(), fakes.PLAN_ARGS)
    deps = make_deps(planner_model=model)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    (correction,) = [e for e in events(deps, run) if e.type == "plan_correction"]
    assert "at least 3" in correction.payload["violations"][0]
    (prompt,) = reprompts(model)
    assert "at least 3" in prompt


def test_a_plan_still_below_three_after_the_reprompt_fails_the_run_with_the_reason(make_deps):
    model = planner_script(two_subtasks(), two_subtasks())
    deps = make_deps(planner_model=model)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert "at least 3" in failure_reason(deps, run)
    assert len(reprompts(model)) == 1  # no third attempt
    assert deps.repos.subtasks.list("alice", run.id) == []


def test_too_many_aspects_asks_to_consolidate_then_fails_without_dropping_any(make_deps):
    model = planner_script(seven_aspects(), seven_aspects())
    deps = make_deps(planner_model=model)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert "7 Aspects" in failure_reason(deps, run)
    assert not any(e.type == "plan_trimmed" for e in events(deps, run))
    (prompt,) = reprompts(model)
    assert "Consolidate" in prompt and "Never drop" in prompt


def test_consolidating_the_aspects_lets_the_run_proceed(make_deps):
    deps = make_deps(planner_model=planner_script(seven_aspects(), fakes.PLAN_ARGS))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE


def test_extra_subtasks_are_trimmed_after_the_reprompt_and_logged(make_deps):
    seven = copy.deepcopy(fakes.PLAN_ARGS)
    seven["plan"]["sub_tasks"] += [
        {"id": f"x{n}", "aspect_id": "a1", "query": fakes.QUERIES["s1"], "source_type": "web",
         "time_horizon_months": 12, "priority": 10 + n} for n in range(4)]
    dispatch = {**fakes.QUERIES, "x0": fakes.QUERIES["s1"], "x1": fakes.QUERIES["s1"],
                "x2": fakes.QUERIES["s1"]}
    model = planner_script(seven, seven, dispatch=dispatch)
    deps = make_deps(planner_model=model)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert len(reprompts(model)) == 1
    (trim,) = [e for e in events(deps, run) if e.type == "plan_trimmed"]
    assert trim.payload["dropped"] == ["x3"]
    assert {r.subtask.id for r in deps.repos.subtasks.list("alice", run.id)} == set(dispatch)


def test_an_uncovered_aspect_fails_the_run_naming_it(make_deps):
    uncovered = copy.deepcopy(fakes.PLAN_ARGS)
    uncovered["plan"]["aspects"].append({"id": "a4", "name": "Moat"})
    deps = make_deps(planner_model=planner_script(uncovered, uncovered))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert "Moat" in failure_reason(deps, run)


def test_a_disallowed_source_type_is_corrected_once(make_deps):
    docs = copy.deepcopy(fakes.PLAN_ARGS)
    docs["plan"]["sub_tasks"][0]["source_type"] = "documents"
    deps = make_deps(planner_model=planner_script(docs, fakes.PLAN_ARGS))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    (correction,) = [e for e in events(deps, run) if e.type == "plan_correction"]
    assert "not allowed: documents" in correction.payload["violations"][0]


def test_research_is_told_exactly_which_subtasks_were_approved(make_deps):
    model = planner_script(fakes.PLAN_ARGS)
    run_brief(make_deps(planner_model=model), "alice", Brief(text=BRIEF))
    (research,) = [str(m.content) for m in model.seen[-1] if str(m.content).startswith("RESEARCH")]
    assert all(f"- {sid}: {q}" in research for sid, q in fakes.QUERIES.items())


def test_an_explicitly_failed_subtask_is_not_retried(make_deps):
    dispatch = {**fakes.QUERIES, "s3": "FAIL-ME"}
    model = planner_script(fakes.PLAN_ARGS, dispatch=dispatch)  # a repair turn would exhaust it
    deps = make_deps(planner_model=model)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert not any(e.type == "repair" for e in events(deps, run))
    status = {r.subtask.id: r.status for r in deps.repos.subtasks.list("alice", run.id)}
    assert status["s3"] == SubTaskStatus.FAILED and status["s1"] == SubTaskStatus.SUCCEEDED


def test_a_missing_subtask_gets_one_repair_round_with_only_its_id(make_deps):
    model = fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",))
    deps = make_deps(planner_model=model)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert [e.payload["missing"] for e in events(deps, run) if e.type == "repair"] == [["s3"]]
    (repair,) = [str(m.content) for m in model.seen[-1] if str(m.content).startswith("REPAIR")]
    assert "- s3:" in repair and "- s1:" not in repair and "- s2:" not in repair


def test_a_brief_with_no_available_source_is_rejected_before_a_run_exists(make_deps):
    deps = make_deps()
    with pytest.raises(SubmissionRejected):
        run_brief(deps, "alice", Brief(text=BRIEF, source_toggles={"documents": True}))
    assert deps.repos.runs.list_for_owner("alice") == []


def test_web_with_unavailable_documents_still_runs(make_deps):
    deps = make_deps()
    brief = Brief(text=BRIEF, source_toggles={"web": True, "documents": True})
    assert run_brief(deps, "alice", brief).state == RunState.COMPLETE
