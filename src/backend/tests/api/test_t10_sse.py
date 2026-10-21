"""T10 AC1 (progress): `GET /research/status` streams a Run's persisted run_events over SSE, with
`seq` as the event id; `Last-Event-ID` resumes strictly after it, so reconnecting loses and
repeats nothing; terminal Runs drain and close; other Users get 404."""

import threading

import pytest

from insightforge_agent.agents.web import FetchError
from insightforge_agent.api import research as research_api
from insightforge_agent.domain.models import RunEvent, RunState
from tests.api.support import T0, App, Gate, LiveStream, read_stream
from tests.pipeline import fakes


class FailingFetches(fakes.FixtureSearch):
    def fetch(self, url):
        raise FetchError(url)


def history(app: App, run_id: str, owner: str = "alice") -> list[tuple[int, str]]:
    return [(e.seq, e.type) for e in app.events(run_id, owner)]


def delivered(frames) -> list[tuple[int, str]]:
    return [(int(f["id"]), f["data"]["type"]) for f in frames if f["event"] == "message"]


def completed_run(app: App, persona: str = "alice") -> str:
    app.login(persona)
    run_id = app.submit().json()["run_id"]
    assert app.wait(run_id, owner=persona).state == RunState.COMPLETE
    return run_id


@pytest.fixture
def app(tmp_path):
    with App(tmp_path) as a:
        yield a


# --- replay from the persisted log ---------------------------------------------------------

def test_a_client_connecting_after_completion_replays_the_whole_history_then_ends(app):
    run_id = completed_run(app)
    response, frames = read_stream(app.client, run_id)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    stored = history(app, run_id)
    assert delivered(frames) == stored  # every event, in persisted order, id = seq
    assert [s for s, _ in stored] == sorted(s for s, _ in stored)
    first = frames[0]["data"]
    assert first["seq"] == int(frames[0]["id"]) and {"type", "payload", "created_at"} <= set(first)
    assert frames[-1] == {"id": None, "event": "end",
                          "data": {"run_id": run_id, "state": "COMPLETE"}}
    stages = [f["data"]["payload"]["state"] for f in frames
              if f["event"] == "message" and f["data"]["type"] == "stage"]
    assert stages[-1] == "COMPLETE"  # the last stage event is delivered before `end`


def test_last_event_id_replays_strictly_after_the_cursor(app):
    run_id = completed_run(app)
    stored = history(app, run_id)
    for cut in (0, 1, len(stored) // 2, len(stored) - 1):
        cursor = stored[cut][0]
        _, frames = read_stream(app.client, run_id, last_event_id=str(cursor))
        assert delivered(frames) == stored[cut + 1:]
        assert frames[-1]["event"] == "end"
    _, everything = read_stream(app.client, run_id, last_event_id="")  # empty: from the start
    assert delivered(everything) == stored


def test_a_failed_run_streams_its_history_and_failure_then_ends(tmp_path):
    with App(tmp_path, Gate(FailingFetches())) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        assert app.wait(run_id).state == RunState.FAILED
        _, frames = read_stream(app.client, run_id)
        assert delivered(frames) == history(app, run_id)
        assert "run_failed" in [t for _, t in delivered(frames)]
        assert frames[-1]["data"] == {"run_id": run_id, "state": "FAILED"}


def test_the_stream_reads_bounded_batches_and_never_the_whole_history(app, monkeypatch):
    run_id = completed_run(app)
    events = app.runtime.repos.events
    for i in range(2 * research_api.BATCH + 7):  # far more events than one batch
        events.append(RunEvent(owner_id="alice", run_id=run_id, type="filler",
                               payload={"i": i}, created_at=T0))
    stored = history(app, run_id)
    limits: list[int] = []
    real = events.read_after

    def spy(owner_id, run_id_, after_seq=0, limit=500):
        limits.append(limit)
        batch = real(owner_id, run_id_, after_seq, limit)
        assert len(batch) <= research_api.BATCH
        return batch

    monkeypatch.setattr(events, "read_after", spy)
    _, frames = read_stream(app.client, run_id)
    assert delivered(frames) == stored
    assert set(limits) == {research_api.BATCH} and len(limits) >= 3


# --- live streaming, reconnect and disconnect ------------------------------------------------

def test_a_live_stream_follows_the_run_to_completion(tmp_path):
    with App(tmp_path, Gate(fakes.FixtureSearch(), open_=False)) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        stream = LiveStream(app.client.app, run_id, app.runtime.session.sign("alice"))
        assert stream.status == 200
        first = stream.frames(limit=1)
        assert first[0]["data"]["type"] == "stage"  # delivered while the Run is held
        assert app.run(run_id).state not in (RunState.COMPLETE, RunState.FAILED)
        app.gate.open()
        rest = stream.frames()
        stream.close()
        assert delivered(first + rest) == history(app, run_id)
        assert rest[-1]["data"]["state"] == "COMPLETE"


def test_reconnecting_with_last_event_id_after_partial_reading_loses_and_repeats_nothing(
        tmp_path):
    with App(tmp_path, Gate(fakes.FixtureSearch(), open_=False)) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        cookie = app.runtime.session.sign("alice")
        first = LiveStream(app.client.app, run_id, cookie)
        part = first.frames(limit=2)
        first.close()  # the connection drops mid-Run
        app.gate.open()
        assert app.wait(run_id).state == RunState.COMPLETE
        again = LiveStream(app.client.app, run_id, cookie, last_event_id=part[-1]["id"])
        rest = again.frames()
        again.close()
        got = delivered(part) + delivered(rest)
        assert got == history(app, run_id)  # no gap, no duplicate
        assert len({s for s, _ in got}) == len(got)


def test_disconnecting_the_stream_does_not_stop_the_run(tmp_path):
    with App(tmp_path, Gate(fakes.FixtureSearch(), open_=False)) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        stream = LiveStream(app.client.app, run_id, app.runtime.session.sign("alice"))
        stream.frames(limit=1)
        stream.close()
        threading.Timer(0.2, app.gate.open).start()
        assert app.wait(run_id).state == RunState.COMPLETE
        assert app.runtime.repos.reports.get_for_run("alice", run_id)


# --- cursor validation and ownership -------------------------------------------------------

@pytest.mark.parametrize("cursor", ["abc", "-1", "1.5", "0x10", "1e3", "+3", "3,4",
                                    "9" * 19])
def test_a_malformed_last_event_id_is_400_and_nothing_is_streamed(app, cursor):
    run_id = completed_run(app)
    response, _ = read_stream(app.client, run_id, last_event_id=cursor)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert "Last-Event-ID" in response.json()["detail"]


def test_another_users_run_and_an_unknown_run_are_the_same_404(app):
    run_id = completed_run(app, "bob")
    app.login("alice")
    foreign, _ = read_stream(app.client, run_id)
    unknown, _ = read_stream(app.client, "run_nope")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["detail"].replace(run_id, "X") == \
        unknown.json()["detail"].replace("run_nope", "X")
    app.login("bob")
    response, frames = read_stream(app.client, run_id)
    assert response.status_code == 200 and delivered(frames) == history(app, run_id, "bob")


def test_the_stream_needs_a_persona(app):
    run_id = completed_run(app)
    app.client.cookies.clear()
    response, _ = read_stream(app.client, run_id)
    assert response.status_code == 401


def test_a_stream_on_an_unfinished_run_closes_after_its_lifetime_and_reconnect_resumes(
        tmp_path):
    """No stream outlives `sse_max_stream_seconds`, so none can hold up a shutdown; it closes
    without `end`, and a reconnect with Last-Event-ID carries on where it stopped."""
    with App(tmp_path, Gate(fakes.FixtureSearch(), open_=False),
             sse_max_stream_seconds=0.3) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        assert app.gate.fetching.wait(30)
        response, first = read_stream(app.client, run_id)  # returns: the stream ended itself
        assert response.status_code == 200 and first
        assert all(f["event"] == "message" for f in first)  # no `end`: the Run is not over
        app.gate.open()
        assert app.wait(run_id).state == RunState.COMPLETE
        _, rest = read_stream(app.client, run_id, last_event_id=first[-1]["id"])
        assert delivered(first) + delivered(rest) == history(app, run_id)
        assert rest[-1]["event"] == "end"


def test_ownership_is_checked_before_the_cursor(app):
    run_id = completed_run(app, "bob")
    app.login("alice")
    response, _ = read_stream(app.client, run_id, last_event_id="not-a-number")
    assert response.status_code == 404


def test_open_streams_end_at_once_when_the_server_begins_shutting_down(tmp_path):
    with App(tmp_path, Gate(fakes.FixtureSearch(), open_=False)) as app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        stream = LiveStream(app.client.app, run_id, app.runtime.session.sign("alice"))
        first = stream.frames(limit=1)
        app.runtime.begin_shutdown()  # what `serve` does on SIGINT / SIGTERM
        rest = stream.frames(timeout=10)
        stream.close()
        assert all(f["event"] == "message" for f in first + rest)  # closed without `end`
        assert app.run(run_id).state not in (RunState.COMPLETE, RunState.FAILED)
