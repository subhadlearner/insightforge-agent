"""T20 B6: the durable Sub-task lifecycle the Run page shows (Queued, Running, Done, Failed).

Three Run-log events, each written from an authoritative point and never inferred:

- `plan_accepted`: the accepted plan, stored in one write with its Sub-task rows.
- `subtask_started`: a dispatch the ApprovedDispatchGuard let through, just before the
  Researcher runs it. A refused dispatch never has one.
- `subtask_finished`: the outcome `collect` validated, stored in one write with the status.
  A Researcher's reply alone is never an outcome; a Run that stops first has none."""

import copy

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import Run, RunState, SubTaskRecord, SubTaskStatus
from insightforge_agent.domain.plan import SubTaskPlan
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.pipeline.recovery import startup_sweep
from tests.api.support import T0
from tests.pipeline import fakes
from tests.pipeline.test_happy_path import BRIEF, events
from tests.pipeline.test_planning import planner_script, researcher_ran, run_with_rogue_s3
from tests.pipeline.test_clarification import (  # noqa: F401 - `world` is a fixture
    ANSWER,
    ask,
    plan_turn,
    research_turn,
    scripted,
    world,
)
from tests.scripted import ScriptedChat

LIFECYCLE = ("plan_accepted", "subtask_started", "subtask_finished")


def lifecycle(deps, run) -> list[tuple[str, dict]]:
    return [(e.type, e.payload) for e in events(deps, run) if e.type in LIFECYCLE]


def of_type(deps, run, type_: str) -> list[dict]:
    return [e.payload for e in events(deps, run) if e.type == type_]


def watch_appends(deps, check) -> None:
    """Calls `check(event)` as each event is appended, before it is stored."""
    real = deps.repos.events.append

    def append(event):
        check(event)
        return real(event)

    deps.repos.events.append = append


# --- Queued: the accepted, persisted plan --------------------------------------------------

def test_the_accepted_plan_is_logged_once_with_exactly_its_stored_subtasks(make_deps):
    """Stored in one write with the plan's rows (tests/stores/test_subtask_state_atomicity.py)."""
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    (plan,) = of_type(deps, run, "plan_accepted")
    assert plan == {"sub_tasks": [
        {"id": sid, "aspect": name, "query": fakes.QUERIES[sid], "source_type": "web"}
        for sid, name in (("s1", "Sales"), ("s2", "Pricing"), ("s3", "Batteries"))]}
    assert [t["id"] for t in plan["sub_tasks"]] == \
        [r.subtask.id for r in deps.repos.subtasks.list("alice", run.id)]
    types = [e.type if e.type != "stage" else e.payload["state"] for e in events(deps, run)]
    assert types.index("plan_accepted") < types.index("RESEARCHING")


def test_a_plan_that_is_never_accepted_logs_no_plan(make_deps):
    two = copy.deepcopy(fakes.PLAN_ARGS)
    two["plan"]["sub_tasks"] = two["plan"]["sub_tasks"][:2]
    deps = make_deps(planner_model=planner_script(two, two))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert lifecycle(deps, run) == []


def test_a_trimmed_plan_logs_only_the_subtasks_it_kept(make_deps):
    seven = copy.deepcopy(fakes.PLAN_ARGS)
    seven["plan"]["sub_tasks"] += [
        {"id": f"x{n}", "aspect_id": "a1", "query": fakes.QUERIES["s1"], "source_type": "web",
         "time_horizon_months": 12, "priority": 10 + n} for n in range(4)]
    kept = {**fakes.QUERIES, "x0": fakes.QUERIES["s1"], "x1": fakes.QUERIES["s1"],
            "x2": fakes.QUERIES["s1"]}
    deps = make_deps(planner_model=planner_script(seven, seven, dispatch=kept))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    (plan,) = of_type(deps, run, "plan_accepted")
    ids = [t["id"] for t in plan["sub_tasks"]]
    assert ids == [r.subtask.id for r in deps.repos.subtasks.list("alice", run.id)]
    assert "x3" not in ids and len(ids) == 6


# --- Running: an approved dispatch that reached its Researcher ----------------------------

def test_each_approved_dispatch_is_started_before_its_researcher_runs(make_deps):
    started_when_researched: list[bool] = []
    researcher = fakes.researcher()
    deps = make_deps(researcher_model=researcher)
    reply = researcher.reply

    def checked(messages):
        if not isinstance(messages[-1], ToolMessage):  # the Researcher's first call
            text = "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
            sid = text.split("SUBTASK_ID=", 1)[1].split()[0]
            started_when_researched.append(any(
                e.type == "subtask_started" and e.payload["subtask_id"] == sid
                for e in deps.repos.events.read_after("alice", run_ids[0])))
        return reply(messages)

    researcher.reply = checked
    run_ids: list[str] = []
    watch_appends(deps, lambda e: run_ids or run_ids.append(e.run_id))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert started_when_researched == [True, True, True]
    assert sorted(p["subtask_id"] for p in of_type(deps, run, "subtask_started")) == \
        ["s1", "s2", "s3"]
    assert {p["phase"] for p in of_type(deps, run, "subtask_started")} == {"research"}


def test_a_refused_dispatch_is_never_started(make_deps):
    made_up = "SUBTASK_ID=s3 something the Planner made up"
    deps, run, researcher = run_with_rogue_s3(make_deps, made_up)
    assert [p["subtask_id"] for p in of_type(deps, run, "dispatch_rejected")] == ["s3"]
    started = {p["subtask_id"] for p in of_type(deps, run, "subtask_started")}
    assert {f"SUBTASK_ID={s}" for s in started} == researcher_ran(researcher)
    assert "s3" not in {p["subtask_id"] for p in of_type(deps, run, "subtask_started")
                        if p["phase"] == "research"}


def test_a_dispatch_to_the_wrong_subagent_is_never_started(make_deps):
    deps, run, researcher = run_with_rogue_s3(make_deps, subagent_type="general-purpose")
    started = [p["subtask_id"] for p in of_type(deps, run, "subtask_started")]
    assert "s3" not in started and {f"SUBTASK_ID={s}" for s in started} == \
        researcher_ran(researcher)


# --- Done and Failed: the outcome collect validated ----------------------------------------

def test_each_logged_outcome_matches_its_stored_status(make_deps):
    """Stored in one write with the status (tests/stores/test_subtask_state_atomicity.py)."""
    plan = copy.deepcopy(fakes.PLAN_ARGS)
    plan["plan"]["sub_tasks"][2]["query"] = "FAIL-ME"
    deps = make_deps(planner_model=planner_script(plan, dispatch={**fakes.QUERIES,
                                                                  "s3": "FAIL-ME"}))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    outcomes = {p["subtask_id"]: p["outcome"] for p in of_type(deps, run, "subtask_finished")}
    assert outcomes == {"s1": "succeeded", "s2": "succeeded", "s3": "failed"}
    assert {r.subtask.id: r.status for r in deps.repos.subtasks.list("alice", run.id)} == {
        "s1": SubTaskStatus.SUCCEEDED, "s2": SubTaskStatus.SUCCEEDED, "s3": SubTaskStatus.FAILED}
    finished = [e.seq for e in events(deps, run) if e.type == "subtask_finished"]
    extracting = [e.seq for e in events(deps, run) if e.type == "stage"
                  and e.payload["state"] == "EXTRACTING"]
    assert max(finished) < extracting[0]


def test_a_researcher_claiming_success_without_usable_sources_is_failed(make_deps):
    """A RESULT reply is not proof: s2 names a Source its search never returned."""
    real = fakes.researcher()

    def reply(messages):
        text = "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
        if "SUBTASK_ID=s2" in text:
            return AIMessage(content="RESULT: found material | sources: src_invented")
        return real.reply(messages)

    deps = make_deps(researcher_model=ScriptedChat(reply=reply))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    outcomes = {p["subtask_id"]: p["outcome"] for p in of_type(deps, run, "subtask_finished")}
    assert outcomes["s2"] == "failed" and outcomes["s1"] == "succeeded"


def test_a_repaired_subtask_keeps_its_identity_and_is_started_again_only_in_repair(make_deps):
    deps = make_deps(planner_model=fakes.planner(research_ids=("s1", "s2"),
                                                 repair_ids=("s3",)))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    started = [(p["subtask_id"], p["phase"]) for p in of_type(deps, run, "subtask_started")]
    assert sorted(started) == [("s1", "research"), ("s2", "research"), ("s3", "repair")]
    types = [e.type for e in events(deps, run)]
    assert types.index("repair") < [i for i, e in enumerate(events(deps, run))
                                    if e.type == "subtask_started"
                                    and e.payload["phase"] == "repair"][0]
    outcomes = {p["subtask_id"]: p["outcome"] for p in of_type(deps, run, "subtask_finished")}
    assert outcomes == {"s1": "succeeded", "s2": "succeeded", "s3": "succeeded"}


def test_a_run_that_stops_before_collecting_fabricates_no_outcome(make_deps):
    """The Planner breaks after dispatching: the Run fails in RESEARCHING with three Sub-tasks
    started and none finished. The stored statuses stay PENDING too."""
    script = fakes.planner().script[:3]  # plan, ack, dispatch; nothing after the results
    deps = make_deps(planner_model=ScriptedChat(script=script))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert sorted(p["subtask_id"] for p in of_type(deps, run, "subtask_started")) == \
        ["s1", "s2", "s3"]
    assert of_type(deps, run, "subtask_finished") == []
    assert {r.status for r in deps.repos.subtasks.list("alice", run.id)} == \
        {SubTaskStatus.PENDING}
    assert of_type(deps, run, "run_failed")


def test_a_question_logs_no_plan_and_the_answer_resumed_after_a_restart_logs_it_once(world):
    """Planning runs twice around the clarifying question; only the run that plans logs a
    plan, and a process restart between the two changes nothing."""
    proc, run = ask(world)
    assert run.state == RunState.AWAITING_CLARIFICATION
    assert [e.type for e in proc.events(run.id) if e.type in LIFECYCLE] == []
    after = proc.restart(scripted(plan_turn(), research_turn()))
    after.service.answer("alice", run.id, ANSWER)
    assert after.run(run.id).state == RunState.COMPLETE
    stored = after.events(run.id)
    assert [e.type for e in stored].count("plan_accepted") == 1
    resumed = next(i for i, e in enumerate(stored) if e.type == "stage"
                   and e.payload["state"] == "RESUMING")
    assert [e.type for e in stored].index("plan_accepted") > resumed


def test_the_startup_sweep_fails_an_interrupted_run_without_inventing_outcomes(world):
    """A process died in RESEARCHING with every Sub-task started: the sweep fails the Run and
    adds no outcome, so the page shows them as not completed."""
    proc = world(scripted())
    run = Run(id="run_cut", owner_id="alice", brief=BRIEF, state=RunState.RESEARCHING,
              created_at=T0)
    proc.repos.runs.add(run)
    plan = SubTaskPlan.model_validate(fakes.PLAN_ARGS["plan"])
    proc.repos.subtasks.save_plan("alice", run.id, [SubTaskRecord(run_id=run.id, subtask=t)
                                                    for t in plan.sub_tasks])
    for t in plan.sub_tasks:
        proc.deps.log("alice", run.id, "subtask_started", subtask_id=t.id, phase="research")
    startup_sweep(proc.deps, proc.service)
    assert proc.run(run.id).state == RunState.FAILED
    assert [e.type for e in proc.events(run.id)][-1] == "run_failed"
    assert "subtask_finished" not in [e.type for e in proc.events(run.id)]
    assert {r.status for r in proc.repos.subtasks.list("alice", run.id)} ==         {SubTaskStatus.PENDING}


def test_lifecycle_payloads_carry_no_transcript_or_tool_arguments(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    allowed = {"plan_accepted": {"sub_tasks"}, "subtask_started": {"subtask_id", "phase"},
               "subtask_finished": {"subtask_id", "outcome"}}
    for type_, payload in lifecycle(deps, run):
        assert set(payload) == allowed[type_]
    for task in of_type(deps, run, "plan_accepted")[0]["sub_tasks"]:
        assert set(task) == {"id", "aspect", "query", "source_type"}
