"""T10 AC1 (submission) and AC5: `POST /research/run` stores the Run and returns at once while
the pipeline runs behind the bounded RunExecutor; capacity, rejection and shutdown are
deterministic, and no accepted Run is left without an outcome."""

import threading
import time

import pytest

from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline import research as research_module
from insightforge_agent.pipeline.executor import (
    BoundedRunExecutor,
    ExecutorClosed,
    ExecutorFull,
)
from insightforge_agent.pipeline.research import NOT_STARTED
from tests.api.support import App, Gate, wait_until
from tests.pipeline import fakes


@pytest.fixture
def held(tmp_path):
    """An app whose Runs stop at their first page fetch until the gate opens."""
    def make(**settings_kw) -> App:
        return App(tmp_path, Gate(fakes.FixtureSearch(), open_=False), **settings_kw)
    return make


# --- submission ----------------------------------------------------------------------------

def test_a_submission_returns_202_with_the_run_before_the_pipeline_runs(held):
    with held() as app:
        app.login("alice")
        started = time.monotonic()
        response = app.submit()
        assert response.status_code == 202
        assert time.monotonic() - started < 5  # the request never waits for the pipeline
        body = response.json()
        run_id = body["run_id"]
        assert body["state"] == "PLANNING"
        assert body["status_url"] == f"/research/status?run_id={run_id}"
        assert app.run(run_id).owner_id == "alice"  # durable before the response
        assert app.gate.fetching.wait(30)
        assert app.run(run_id).state == RunState.RESEARCHING  # held mid-pipeline
        app.gate.open()
        assert app.wait(run_id).state == RunState.COMPLETE


def test_the_pipeline_runs_on_an_executor_worker_and_never_in_the_request(
        tmp_path, monkeypatch):
    carried_on: list[str] = []
    real_carry = research_module.carry_run

    def carry(deps, started, on_state=None):
        carried_on.append(threading.current_thread().name)
        return real_carry(deps, started, on_state)

    monkeypatch.setattr(research_module, "carry_run", carry)
    with App(tmp_path) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        assert app.wait(run_id).state == RunState.COMPLETE
        assert len(carried_on) == 1 and carried_on[0].startswith("run_")  # a RunExecutor worker
        wait_until(lambda: app.runtime.executor.in_flight == 0)


@pytest.mark.parametrize("body", [
    {"source_toggles": {"web": False, "documents": False, "memory": False}},
    {"source_toggles": {"web": False, "documents": True}},  # documents, but none uploaded
    {"source_toggles": {"web": False, "memory": True}},  # memory, but no eligible Report
])
def test_a_brief_with_no_available_source_type_is_422_and_creates_no_run(tmp_path, body):
    with App(tmp_path) as app:
        app.login("alice")
        response = app.submit(**body)
        assert response.status_code == 422
        assert app.runtime.repos.runs.list_for_owner("alice") == []
        assert app.runtime.executor.in_flight == 0


@pytest.mark.parametrize("body", [
    {"brief": ""},
    {"source_toggles": {"satellite": True}},
    {"documents": [{"filename": "a.pdf", "content_base64": "not base64!"}]},
    {"documents": [{"filename": "a.txt", "content_base64": "aGVsbG8="}]},  # not a PDF
])
def test_an_invalid_submission_is_422_and_creates_no_run(tmp_path, body):
    with App(tmp_path) as app:
        app.login("alice")
        response = app.client.post("/research/run", json={"brief": fakes.QUERIES["s1"], **body})
        assert response.status_code == 422
        assert app.runtime.repos.runs.list_for_owner("alice") == []


def test_submitting_needs_a_persona_and_the_run_belongs_to_it(tmp_path):
    with App(tmp_path) as app:
        assert app.submit().status_code == 401
        app.login("bob")
        run_id = app.submit().json()["run_id"]
        assert app.wait(run_id, owner="bob").owner_id == "bob"
        assert app.runtime.repos.runs.list_for_owner("alice") == []


# --- capacity -----------------------------------------------------------------------------

def test_a_full_executor_refuses_with_503_and_creates_no_run(held):
    with held(run_concurrency=1, run_queue_limit=0) as app:
        app.login("alice")
        first = app.submit().json()["run_id"]
        assert app.gate.fetching.wait(30)
        refused = app.submit()
        assert refused.status_code == 503
        assert refused.headers["retry-after"].isdigit()
        assert "capacity" in refused.json()["detail"]
        assert [r.id for r in app.runtime.repos.runs.list_for_owner("alice")] == [first]
        app.gate.open()
        assert app.wait(first).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        again = app.submit()  # capacity is back
        assert again.status_code == 202
        assert app.wait(again.json()["run_id"]).state == RunState.COMPLETE


def test_a_new_run_waits_in_the_bounded_queue_then_runs(held):
    with held(run_concurrency=1, run_queue_limit=1) as app:
        app.login("alice")
        first = app.submit().json()["run_id"]
        assert app.gate.fetching.wait(30)
        queued = app.submit()
        assert queued.status_code == 202
        queued_id = queued.json()["run_id"]
        time.sleep(0.2)
        assert app.run(queued_id).state == RunState.PLANNING  # stored, not yet running
        assert app.events(queued_id) == []
        assert app.submit().status_code == 503  # 1 running + 1 queued is the capacity
        app.gate.open()
        assert app.wait(first).state == RunState.COMPLETE
        assert app.wait(queued_id).state == RunState.COMPLETE


def test_the_executor_never_runs_more_than_its_workers_at_once():
    executor = BoundedRunExecutor(max_workers=2, max_queued=10)
    lock, running, peak = threading.Lock(), [0], [0]
    threads: set[str] = set()

    def work(_):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            threads.add(threading.current_thread().name)
        time.sleep(0.05)
        with lock:
            running[0] -= 1

    for i in range(12):
        executor.admit(lambda i=i: i, work, lambda _: None)
    with pytest.raises(ExecutorFull):
        executor.admit(lambda: "13th", work, lambda _: None)
    executor.shutdown()
    assert peak[0] == 2 and len(threads) == 2


def test_resumes_of_accepted_runs_are_never_refused_for_capacity():
    executor = BoundedRunExecutor(max_workers=1, max_queued=0)
    release = threading.Event()
    executor.admit(lambda: "new", lambda _: release.wait(10), lambda _: None)
    with pytest.raises(ExecutorFull):
        executor.admit(lambda: "another new Run", lambda _: None, lambda _: None)
    done = executor.submit(lambda: "resumed")  # already accepted work: queued, not refused
    release.set()
    assert done.result(10) == "resumed"
    executor.shutdown()


# --- rejection and shutdown never strand a Run ---------------------------------------------

def test_a_refused_admission_never_calls_create():
    executor = BoundedRunExecutor(max_workers=1)
    executor.shutdown()
    created = []
    with pytest.raises(ExecutorClosed):
        executor.admit(lambda: created.append(1), lambda _: None, lambda _: None)
    with pytest.raises(ExecutorClosed):
        executor.submit(lambda: None)
    assert created == []


def test_a_run_created_as_shutdown_begins_is_settled_failed_not_left_waiting(
        tmp_path, monkeypatch):
    with App(tmp_path) as app:
        app.login("alice")
        real_start = research_module.start_run

        def start_then_shut_down(deps, checked):
            started = real_start(deps, checked)
            app.runtime.executor.shutdown()  # shutdown wins the race to the queue
            return started

        monkeypatch.setattr(research_module, "start_run", start_then_shut_down)
        response = app.submit()
        assert response.status_code == 503 and "shutting down" in response.json()["detail"]
        [run] = app.runtime.repos.runs.list_for_owner("alice")
        assert run.state == RunState.FAILED
        assert [e.payload["reason"] for e in app.events(run.id) if e.type == "run_failed"] == [
            NOT_STARTED]


def test_shutdown_finishes_running_runs_and_settles_queued_ones(held):
    with held(run_concurrency=1, run_queue_limit=1) as app:
        app.login("alice")
        running = app.submit().json()["run_id"]
        assert app.gate.fetching.wait(30)
        queued = app.submit().json()["run_id"]
        stopper = threading.Thread(target=app.runtime.executor.shutdown)
        stopper.start()
        wait_until(lambda: app.run(queued).state == RunState.FAILED)  # cancelled at once
        assert stopper.is_alive()  # still waiting for the running Run
        assert app.submit().status_code == 503
        app.gate.open()
        stopper.join(60)
        assert not stopper.is_alive()
        assert app.run(running).state == RunState.COMPLETE  # accepted work was not dropped
        reasons = [e.payload["reason"] for e in app.events(queued) if e.type == "run_failed"]
        assert reasons == [NOT_STARTED]
