"""T9: a Vague Brief gets one clarifying question; the Run waits (LangGraph interrupt, durable
checkpoint), is resolved by the answer or the timeout through one compare-and-set, resumes
through RESUMING, and survives restarts (the startup sweep).

Everything runs on SQLite files (repositories and the LangGraph checkpointer), so a "restart"
is a new connection, a new graph and new models over the same files. Models are scripted; a
scripted model raises if it is called more often than scripted, which proves no step runs
twice. The clock is fake."""

import copy
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.api.clarify import clarification_router
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.errors import ClarificationConflict, NotFoundError
from insightforge_agent.domain.models import Run, RunState
from insightforge_agent.pipeline.clarification import ClarificationService
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.graph import (
    build_graph,
    outer_config,
    resume_run,
    run_brief,
    run_thread_id,
)
from insightforge_agent.pipeline.memory import eligible_report_ids
from insightforge_agent.pipeline.recovery import startup_sweep
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, ai, call

VAGUE = "Tell me about the market."
QUESTION = "Which market, region and time period should the research cover?"
ANSWER = "Electric vehicles in China and the US, 2025: BYD and Tesla sales, pricing, batteries."
T0 = datetime(2030, 3, 1, 9, 0, tzinfo=UTC)
TIMEOUT = timedelta(minutes=30)


# --- scripted Planner turns ---------------------------------------------------------------

def ask_turn() -> list:
    return [ai(call("ask_clarification", {"question": QUESTION}, "ask-1")),
            ai(text="Waiting for the answer.")]


def plan_turn(args=None, assumption: str = "", n: int = 0) -> list:
    args = copy.deepcopy(args or fakes.PLAN_ARGS)
    if assumption:
        args["assumption"] = assumption
    return [ai(call("submit_plan", args, f"plan-{n}")), ai(text="Plan submitted.")]


def research_turn() -> list:
    todos = call("write_todos", {"todos": [
        {"content": q, "status": "pending"} for q in fakes.QUERIES.values()]}, "todo-1")
    return [ai(todos, *[fakes.task(s, f"research-{s}") for s in fakes.QUERIES]),
            ai(text="Dispatched.")]


def scripted(*turns) -> ScriptedChat:
    return ScriptedChat(script=[m for t in turns for m in t])


def two_subtasks() -> dict:
    args = copy.deepcopy(fakes.PLAN_ARGS)
    args["plan"]["sub_tasks"] = args["plan"]["sub_tasks"][:2]
    return args


# --- a process over durable files -----------------------------------------------------------

class Clock:
    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, delta: timedelta) -> None:
        self.at += delta


class RecordingTimeouts:
    """A TimeoutScheduler that records what is scheduled; the APScheduler adapter has its own
    tests below and in tests/test_t9_scheduler.py."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], datetime] = {}
        self.scheduled: list[tuple[str, str, datetime]] = []
        self.cancelled: list[tuple[str, str]] = []

    def schedule(self, owner_id, run_id, at):
        self.jobs[(owner_id, run_id)] = at
        self.scheduled.append((owner_id, run_id, at))

    def cancel(self, owner_id, run_id):
        self.jobs.pop((owner_id, run_id), None)
        self.cancelled.append((owner_id, run_id))


class Process:
    """One process lifetime over the database files in `tmp_path`: its own connections, models
    and (unless shared) clock. `restart()` returns a new one over the same files."""

    def __init__(self, tmp_path, stack: ExitStack, planner: ScriptedChat, clock: Clock,
                 timeouts: RecordingTimeouts | None = None, **overrides) -> None:
        self.tmp_path, self.stack, self.clock = tmp_path, stack, clock
        self.repos = SqliteRepos(str(tmp_path / "app.db"))
        stack.callback(self.repos.close)
        saver = stack.enter_context(SqliteSaver.from_conn_string(str(tmp_path / "ck.db")))
        search = fakes.FixtureSearch()
        self.timeouts = timeouts if timeouts is not None else RecordingTimeouts()
        parts = dict(
            repos=self.repos,
            store=SourceStore(self.repos.sources, self.repos.observations, self.repos.passages),
            planner_model=planner, writer_model=fakes.writer(), light_model=fakes.light(),
            researcher_model=fakes.researcher(), search=search, fetcher=search,
            checkpointer=saver, now=clock, timeouts=self.timeouts,
            clarification_timeout=TIMEOUT)
        parts.update(overrides)
        self.deps = Deps(**parts)
        self.submitted: list = []
        self.service = ClarificationService(self.deps, submit=self.run_now)

    def run_now(self, work):
        self.submitted.append(work)
        return work()

    def restart(self, planner: ScriptedChat, **kw) -> "Process":
        """A new process over the same files and the same clock."""
        return Process(self.tmp_path, self.stack, planner, self.clock, **kw)

    def events(self, run_id, owner="alice"):
        return self.repos.events.read_after(owner, run_id, 0, 10_000)

    def stages(self, run_id):
        return [e.payload["state"] for e in self.events(run_id) if e.type == "stage"]

    def run(self, run_id, owner="alice") -> Run:
        return self.repos.runs.get(owner, run_id)


@pytest.fixture
def world(tmp_path):
    with ExitStack() as stack:
        def make(planner: ScriptedChat, clock: Clock | None = None, **kw) -> Process:
            return Process(tmp_path, stack, planner, clock or Clock(), **kw)
        yield make


def ask(world, **kw) -> tuple[Process, Run]:
    """A process whose Planner asks the question; returns the paused Run."""
    proc = world(scripted(ask_turn()), **kw)
    return proc, run_brief(proc.deps, "alice", Brief(text=VAGUE))


def api(proc: Process) -> TestClient:
    """The router as T10 will mount it; a header stands in for T10's persona cookie."""
    def user(x_user: str = Header()) -> str:
        return x_user

    app = FastAPI()
    app.include_router(clarification_router(proc.service, user))
    return TestClient(app)


# --- AC1: interrupt, checkpoint, one question ----------------------------------------------

def test_a_concrete_brief_plans_without_a_question(world):
    proc = world(scripted(plan_turn(), research_turn()))
    run = run_brief(proc.deps, "alice", Brief(text=fakes.QUERIES["s1"]))
    assert run.state == RunState.COMPLETE
    assert run.clarification is None and proc.timeouts.scheduled == []
    assert "AWAITING_CLARIFICATION" not in proc.stages(run.id)


def test_a_vague_brief_waits_for_its_one_question_with_the_checkpoint_in_the_database(world):
    proc, run = ask(world)
    assert run.state == RunState.AWAITING_CLARIFICATION
    stored = proc.run(run.id)
    assert stored.state == RunState.AWAITING_CLARIFICATION  # non-terminal, not FAILED
    assert stored.clarification.question == QUESTION
    assert stored.clarification.resolution is None
    types = [e.type for e in proc.events(run.id)]
    assert types.count("clarification_requested") == 1 and "run_failed" not in types
    assert proc.stages(run.id)[-1] == "AWAITING_CLARIFICATION"
    # The pause is a LangGraph interrupt on the outer graph's checkpoint, in the SQLite file.
    paused = build_graph(proc.deps).get_state(outer_config(run_thread_id("alice", run.id)))
    assert paused.next == ("clarify",)
    assert [i.value for i in paused.interrupts] == [{"question": QUESTION}]
    assert proc.service.question("alice", run.id).question == QUESTION


def test_the_deadline_is_the_entry_time_plus_the_timeout_and_one_job_is_scheduled(world):
    proc, run = ask(world, clock=Clock(T0))
    c = proc.run(run.id).clarification
    assert c.asked_at == T0 and c.deadline == T0 + TIMEOUT
    assert proc.timeouts.scheduled == [("alice", run.id, T0 + TIMEOUT)]


def test_the_default_clarification_timeout_is_thirty_minutes():
    from insightforge_agent.config import Settings

    assert Deps.__dataclass_fields__["clarification_timeout"].default == timedelta(minutes=30)
    assert Settings(llm_provider="fake", search_providers=["fake"]).clarification_timeout_minutes == 30


def test_the_checkpoint_survives_a_restart_and_the_answer_resumes_it(world):
    proc, run = ask(world)
    # New process: new connections, graph and models. Its Planner is scripted only for what
    # comes after the answer, so asking again or replanning from scratch would fail the test.
    after = proc.restart(scripted(plan_turn(), research_turn()))
    after.clock.advance(timedelta(minutes=10))
    after.service.answer("alice", run.id, ANSWER)
    final = after.run(run.id)
    assert final.state == RunState.COMPLETE, [e.payload for e in after.events(run.id)]
    assert final.clarification.resolution == "answered" and final.clarification.answer == ANSWER
    assert len(after.repos.reports.list_for_owner("alice")) == 1
    # The Planner saw the answer on the same thread as its question.
    seen = after.deps.planner_model.seen[0]
    texts = [str(m.content) for m in seen if m.type == "human"]
    assert texts[0].startswith("PLAN. ") and VAGUE in texts[0]
    assert texts[1].startswith("PLAN (clarified)") and ANSWER in texts[1]


def test_an_answered_run_goes_through_resuming_back_into_planning(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    proc.service.answer("alice", run.id, ANSWER)
    stages = proc.stages(run.id)
    i = stages.index("AWAITING_CLARIFICATION")
    assert stages[i:i + 3] == ["AWAITING_CLARIFICATION", "RESUMING", "PLANNING"]
    assert stages[-1] == "COMPLETE"
    assert ("alice", run.id) in proc.timeouts.cancelled  # the answer cancels the job


def test_the_planner_may_not_ask_a_second_question(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += (
        [ai(call("ask_clarification", {"question": "And which year?"}, "ask-2")),
         ai(text="Waiting.")] + plan_turn(n=1) + research_turn())
    proc.service.answer("alice", run.id, ANSWER)
    assert proc.run(run.id).state == RunState.COMPLETE
    refused = [e.payload for e in proc.events(run.id) if e.type == "clarification_refused"]
    assert [r["question"] for r in refused] == ["And which year?"]
    assert proc.stages(run.id).count("AWAITING_CLARIFICATION") == 1
    assert len(proc.timeouts.scheduled) == 1


def test_a_watchlist_run_never_waits_for_an_answer(world):
    proc = world(scripted(ask_turn(), plan_turn(), research_turn()))
    run = run_brief(proc.deps, "alice", Brief(text=VAGUE), watchlist_item_id="wl_1")
    assert run.state == RunState.COMPLETE and run.clarification is None
    assert any(e.type == "clarification_refused" for e in proc.events(run.id))


def test_without_a_timeout_scheduler_the_question_is_refused_rather_than_left_waiting(world):
    proc = world(scripted(ask_turn(), plan_turn(), research_turn()), timeouts=None)
    proc.deps.timeouts = None
    run = run_brief(proc.deps, "alice", Brief(text=VAGUE))
    assert run.state == RunState.COMPLETE and run.clarification is None


# --- AC3: one compare-and-set for the answer and the timeout --------------------------------

def test_an_answer_before_the_deadline_wins_and_a_later_timeout_changes_nothing(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    proc.clock.advance(timedelta(minutes=5))
    proc.service.answer("alice", run.id, ANSWER)
    proc.clock.advance(TIMEOUT)
    assert proc.service.time_out("alice", run.id) is False  # a job that fires late anyway
    c = proc.run(run.id).clarification
    assert (c.resolution, c.answer) == ("answered", ANSWER)
    assert len(proc.submitted) == 1


def test_the_timeout_wins_when_it_comes_first_and_a_late_answer_is_refused(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn(assumption="the global EV market") + research_turn()
    proc.clock.advance(TIMEOUT)
    assert proc.service.time_out("alice", run.id) is True
    with pytest.raises(ClarificationConflict):
        proc.service.answer("alice", run.id, ANSWER)
    assert proc.run(run.id).clarification.resolution == "timed_out"
    assert len(proc.submitted) == 1


def test_a_timeout_job_firing_before_the_deadline_changes_nothing(world):
    proc, run = ask(world)
    proc.clock.advance(TIMEOUT - timedelta(seconds=1))
    assert proc.service.time_out("alice", run.id) is False
    assert proc.run(run.id).state == RunState.AWAITING_CLARIFICATION


def test_an_answer_after_the_deadline_is_late_even_before_the_job_runs(world):
    proc, run = ask(world)
    proc.clock.advance(TIMEOUT)
    with pytest.raises(ClarificationConflict):
        proc.service.answer("alice", run.id, ANSWER)
    assert proc.run(run.id).state == RunState.AWAITING_CLARIFICATION  # the timeout resolves it


def test_concurrent_answer_and_timeout_have_exactly_one_winner_and_one_resume(world, tmp_path):
    """The answer arrives a moment before the deadline and the job fires at it, on two
    threads with their own connections: both pass their own checks, and only the
    compare-and-set decides."""
    proc, run = ask(world)
    late = Clock(T0 + TIMEOUT - timedelta(milliseconds=1))
    due = Clock(T0 + TIMEOUT)
    a = Process(tmp_path, proc.stack, ScriptedChat(script=[]), late)
    b = Process(tmp_path, proc.stack, ScriptedChat(script=[]), due)
    for p in (a, b):
        p.service = ClarificationService(p.deps, submit=p.submitted.append)  # resume recorded
    start, outcomes = threading.Barrier(2), {}

    def answer():
        start.wait()
        try:
            a.service.answer("alice", run.id, ANSWER)
            outcomes["answer"] = "won"
        except ClarificationConflict:
            outcomes["answer"] = "409"

    def timeout():
        start.wait()
        outcomes["timeout"] = "won" if b.service.time_out("alice", run.id) else "lost"

    threads = [threading.Thread(target=answer), threading.Thread(target=timeout)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes.values()) in (["409", "won"], ["lost", "won"]), outcomes
    assert len(a.submitted) + len(b.submitted) == 1  # exactly one resume
    c = proc.run(run.id).clarification
    assert c.resolution == ("answered" if outcomes["answer"] == "won" else "timed_out")


def test_the_endpoint_answers_202_then_409_for_a_duplicate(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    client = api(proc)
    got = client.get(f"/research/{run.id}/clarification", headers={"x-user": "alice"})
    assert got.status_code == 200 and got.json()["question"] == QUESTION
    first = client.post(f"/research/{run.id}/clarify", json={"answer": ANSWER},
                        headers={"x-user": "alice"})
    assert first.status_code == 202 and first.json()["state"] == "RESUMING"
    again = client.post(f"/research/{run.id}/clarify", json={"answer": "something else"},
                        headers={"x-user": "alice"})
    assert again.status_code == 409
    assert proc.run(run.id).clarification.answer == ANSWER


def test_the_endpoint_answers_409_after_a_timeout(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    proc.clock.advance(TIMEOUT)
    proc.service.time_out("alice", run.id)
    late = api(proc).post(f"/research/{run.id}/clarify", json={"answer": ANSWER},
                          headers={"x-user": "alice"})
    assert late.status_code == 409
    assert proc.run(run.id).clarification.resolution == "timed_out"


def test_another_user_cannot_see_or_answer_the_question(world):
    proc, run = ask(world)
    client = api(proc)
    assert client.get(f"/research/{run.id}/clarification",
                      headers={"x-user": "bob"}).status_code == 404
    assert client.post(f"/research/{run.id}/clarify", json={"answer": ANSWER},
                       headers={"x-user": "bob"}).status_code == 404
    with pytest.raises(NotFoundError):
        proc.service.answer("bob", run.id, ANSWER)
    assert proc.run(run.id).state == RunState.AWAITING_CLARIFICATION


def test_an_empty_answer_is_rejected_and_changes_nothing(world):
    proc, run = ask(world)
    client = api(proc)
    for body in ({"answer": ""}, {"answer": "x" * 2001}):
        assert client.post(f"/research/{run.id}/clarify", json=body,
                           headers={"x-user": "alice"}).status_code == 422
    with pytest.raises(ValueError):
        proc.service.answer("alice", run.id, "   ")
    assert proc.run(run.id).state == RunState.AWAITING_CLARIFICATION


def test_a_run_that_never_asked_cannot_be_answered(world):
    proc = world(scripted(plan_turn(), research_turn()))
    run = run_brief(proc.deps, "alice", Brief(text=fakes.QUERIES["s1"]))
    assert api(proc).post(f"/research/{run.id}/clarify", json={"answer": ANSWER},
                          headers={"x-user": "alice"}).status_code == 409


# --- AC4: the resolution first, then RESUMING; timeout best-effort ---------------------------

def test_a_timed_out_run_plans_best_effort_and_records_the_assumption(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += (
        plan_turn(assumption="EV makers BYD and Tesla, 2025") + research_turn())
    proc.clock.advance(TIMEOUT)
    proc.service.time_out("alice", run.id)
    assert proc.run(run.id).state == RunState.COMPLETE
    seen = proc.deps.planner_model.seen
    timed_out = [str(m.content) for conv in seen for m in conv if m.type == "human"
                 and str(m.content).startswith("PLAN (clarification timed out)")]
    assert timed_out and QUESTION in timed_out[0]
    gaps = proc.repos.reports.get_for_run("alice", run.id).body["draft"]["gaps"]
    assert gaps[0] == (f'The clarifying question "{QUESTION}" was not answered before its '
                       "deadline, so the plan rests on a best-effort reading of the Brief: "
                       "EV makers BYD and Tesla, 2025")
    summary = proc.repos.summaries.get_for_run("alice", run.id)
    assert summary.gaps[0].startswith("The clarifying question")
    assert [e.payload["detail"] for e in proc.events(run.id) if e.type == "plan_assumption"]


def test_a_timeout_with_no_stated_assumption_still_records_the_limitation(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    proc.clock.advance(TIMEOUT)
    proc.service.time_out("alice", run.id)
    gaps = proc.repos.reports.get_for_run("alice", run.id).body["draft"]["gaps"]
    assert gaps[0].endswith("a best-effort reading of the Brief.")


def test_an_answered_run_records_no_assumption(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    proc.service.answer("alice", run.id, ANSWER)
    gaps = proc.repos.reports.get_for_run("alice", run.id).body["draft"]["gaps"]
    assert not any("clarifying question" in g for g in gaps)


def test_plan_bounds_still_apply_after_the_answer(world):
    """T4: one corrective re-prompt, then a plan with too few Sub-tasks fails the Run."""
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn(two_subtasks()) + plan_turn(two_subtasks(), n=1)
    proc.service.answer("alice", run.id, ANSWER)
    assert proc.run(run.id).state == RunState.FAILED
    corrections = [e for e in proc.events(run.id) if e.type == "plan_correction"]
    assert len(corrections) == 1 and "needs at least 3" in corrections[0].payload["violations"][0]
    (failed,) = [e for e in proc.events(run.id) if e.type == "run_failed"]
    assert "needs at least 3" in failed.payload["reason"]


def test_a_correction_after_the_answer_can_still_succeed(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += (plan_turn(two_subtasks()) + plan_turn(n=1)
                                       + research_turn())
    proc.service.answer("alice", run.id, ANSWER)
    assert proc.run(run.id).state == RunState.COMPLETE


def test_a_failed_run_after_a_timeout_has_no_report_and_is_never_memory(world):
    proc, run = ask(world)
    proc.deps.planner_model.script += plan_turn(two_subtasks()) + plan_turn(two_subtasks(), n=1)
    proc.clock.advance(TIMEOUT)
    proc.service.time_out("alice", run.id)
    assert proc.run(run.id).state == RunState.FAILED
    assert proc.repos.reports.list_for_owner("alice") == []
    with pytest.raises(NotFoundError):
        proc.repos.summaries.get_for_run("alice", run.id)
    assert eligible_report_ids(proc.deps, "alice", None) == []


def test_an_awaiting_run_is_never_memory_or_planner_context(world):
    proc, run = ask(world)
    assert eligible_report_ids(proc.deps, "alice", None) == []
    assert proc.repos.reports.list_for_owner("alice") == []
    assert proc.repos.summaries.list_for("alice", None) == []


# --- AC5: the startup sweep ---------------------------------------------------------------

def test_a_crash_after_the_resolution_is_stored_resumes_on_restart(world):
    proc, run = ask(world)
    # The answer is stored, then the process dies before the resume runs.
    proc.service = ClarificationService(proc.deps, submit=lambda work: None)
    proc.service.answer("alice", run.id, ANSWER)
    assert proc.run(run.id).state == RunState.RESUMING
    after = proc.restart(scripted(plan_turn(), research_turn()))
    assert startup_sweep(after.deps, after.service) == {"resumed": 1}
    final = after.run(run.id)
    assert final.state == RunState.COMPLETE and final.clarification.answer == ANSWER
    texts = [str(m.content) for m in after.deps.planner_model.seen[0] if m.type == "human"]
    assert ANSWER in texts[-1]  # resumed with the stored answer, not asked again


def test_a_repeated_sweep_never_runs_a_run_twice(world):
    proc, run = ask(world)
    proc.service = ClarificationService(proc.deps, submit=lambda work: None)
    proc.service.answer("alice", run.id, ANSWER)
    after = proc.restart(scripted(plan_turn(), research_turn()))  # scripted for one run only
    startup_sweep(after.deps, after.service)
    again = after.restart(ScriptedChat(script=[]))  # any Planner call would raise
    assert startup_sweep(again.deps, again.service) == {}
    assert len(again.repos.reports.list_for_owner("alice")) == 1
    assert again.run(run.id).state == RunState.COMPLETE


def test_resuming_twice_carries_the_run_on_once(world):
    proc, run = ask(world)
    proc.service = ClarificationService(proc.deps, submit=lambda work: None)
    proc.service.answer("alice", run.id, ANSWER)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    assert resume_run(proc.deps, "alice", run.id).state == RunState.COMPLETE
    assert resume_run(proc.deps, "alice", run.id).state == RunState.COMPLETE  # no-op
    assert proc.stages(run.id).count("COMPLETE") == 1


def test_an_expired_awaiting_run_times_out_during_the_sweep(world):
    proc, run = ask(world)
    after = proc.restart(scripted(plan_turn(assumption="global EV makers"), research_turn()))
    after.clock.advance(TIMEOUT + timedelta(hours=2))  # the process was down past the deadline
    assert startup_sweep(after.deps, after.service) == {"timed_out": 1}
    final = after.run(run.id)
    assert final.state == RunState.COMPLETE and final.clarification.resolution == "timed_out"
    gaps = after.repos.reports.get_for_run("alice", run.id).body["draft"]["gaps"]
    assert "global EV makers" in gaps[0]


def test_an_unexpired_awaiting_run_is_left_waiting_and_its_timeout_rescheduled(world):
    proc, run = ask(world)
    after = proc.restart(ScriptedChat(script=[]))
    after.clock.advance(TIMEOUT - timedelta(minutes=1))
    assert startup_sweep(after.deps, after.service) == {"still_awaiting": 1}
    assert after.run(run.id).state == RunState.AWAITING_CLARIFICATION
    assert after.timeouts.scheduled == [("alice", run.id, T0 + TIMEOUT)]
    assert after.submitted == []


def test_ordinary_runs_left_in_progress_fail_and_terminal_runs_are_untouched(world):
    proc = world(ScriptedChat(script=[]))
    states = [RunState.INGESTING, RunState.PLANNING, RunState.RESEARCHING, RunState.EXTRACTING,
              RunState.SYNTHESIZING, RunState.WRITING, RunState.FACT_CHECKING,
              RunState.COMPLETE, RunState.FAILED]
    for n, state in enumerate(states):
        proc.repos.runs.add(Run(id=f"run_{n}", owner_id="alice", brief="b", state=state,
                                created_at=T0))
    assert startup_sweep(proc.deps, proc.service) == {"failed": 7}
    for n, state in enumerate(states):
        expected = state if state in (RunState.COMPLETE, RunState.FAILED) else RunState.FAILED
        assert proc.run(f"run_{n}").state == expected
    failed = [e for n in range(9) for e in proc.events(f"run_{n}") if e.type == "run_failed"]
    assert len(failed) == 7 and "process stopped" in failed[0].payload["reason"]
    assert startup_sweep(proc.deps, proc.service) == {}


def test_a_run_crashed_while_planning_after_its_resume_fails_rather_than_asking_again(world):
    proc, run = ask(world)
    proc.service = ClarificationService(proc.deps, submit=lambda work: None)
    proc.service.answer("alice", run.id, ANSWER)
    proc.repos.runs.claim_resume("alice", run.id)  # claimed, then the process died
    after = proc.restart(ScriptedChat(script=[]))
    assert startup_sweep(after.deps, after.service) == {"failed": 1}
    assert after.run(run.id).state == RunState.FAILED


def test_a_resuming_run_whose_checkpoint_took_the_answer_carries_on_from_there(world):
    """Defensive path of `resume_run`: the checkpoint is already past the question."""
    proc, run = ask(world)
    proc.service = ClarificationService(proc.deps, submit=lambda work: None)
    proc.service.answer("alice", run.id, ANSWER)
    from langgraph.types import Command

    config = outer_config(run_thread_id("alice", run.id))
    build_graph(proc.deps, interrupt_after=["clarify"]).invoke(
        Command(resume={"question": QUESTION, "resolution": "answered", "answer": ANSWER}),
        config)
    assert build_graph(proc.deps).get_state(config).next == ("planning",)
    proc.deps.planner_model.script += plan_turn() + research_turn()
    assert resume_run(proc.deps, "alice", run.id).state == RunState.COMPLETE


def test_a_resuming_run_without_a_checkpoint_fails(world):
    proc, run = ask(world)
    proc.service = ClarificationService(proc.deps, submit=lambda work: None)
    proc.service.answer("alice", run.id, ANSWER)
    with SqliteSaver.from_conn_string(":memory:") as empty:
        lost = proc.restart(ScriptedChat(script=[]), checkpointer=empty)
        assert resume_run(lost.deps, "alice", run.id).state == RunState.FAILED
