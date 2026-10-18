"""T20 B6: `GET /research/runs/{run_id}` gives the Run page what its elapsed time is measured
from: the stored `Run.created_at` (so queueing and clarification count) and the server's clock
now (so a browser clock that is off does not skew it). Read only, and the owner's alone."""

from datetime import timedelta

from insightforge_agent.domain.models import RunState
from tests.api.support import T0, App
from tests.api.test_t10_sse import completed_run


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self):
        return self.at


def test_the_owner_gets_the_stored_creation_time_and_the_server_clock(tmp_path):
    clock = Clock()
    with App(tmp_path, now=clock) as app:
        run_id = completed_run(app)
        clock.at = T0 + timedelta(minutes=7)
        response = app.client.get(f"/research/runs/{run_id}")
        assert response.status_code == 200
        body = response.json()
        stored = app.run(run_id)
        assert body == {"run_id": run_id, "state": "COMPLETE",
                        "created_at": stored.created_at.isoformat().replace("+00:00", "Z"),
                        "now": (T0 + timedelta(minutes=7)).isoformat().replace("+00:00", "Z")}
        assert stored.created_at == T0


def test_another_users_run_and_an_unknown_run_are_the_same_404(tmp_path):
    with App(tmp_path) as app:
        run_id = completed_run(app, "bob")
        app.login("alice")
        foreign = app.client.get(f"/research/runs/{run_id}")
        unknown = app.client.get("/research/runs/run_nope")
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json()["detail"].replace(run_id, "X") == \
            unknown.json()["detail"].replace("run_nope", "X")


def test_it_needs_a_persona_and_creates_nothing(tmp_path):
    with App(tmp_path) as app:
        run_id = completed_run(app)
        runs = app.runtime.repos.runs.list_for_owner("alice")
        for _ in range(3):  # a reload asks again; no Run is ever submitted by it
            assert app.client.get(f"/research/runs/{run_id}").status_code == 200
        assert app.runtime.repos.runs.list_for_owner("alice") == runs
        assert app.run(run_id).state == RunState.COMPLETE
        app.client.cookies.clear()
        assert app.client.get(f"/research/runs/{run_id}").status_code == 401
