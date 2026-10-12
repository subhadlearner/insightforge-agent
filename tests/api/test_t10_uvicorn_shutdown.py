"""T10: graceful shutdown under a real uvicorn process with an SSE stream open on an unfinished
Run. The TestClient lifecycle tests cannot show how uvicorn treats an active streaming
connection when it is told to stop; this does. Offline: scripted models, loopback only.

Measured: uvicorn waits for open connections to close before the app's shutdown runs, so under
plain `uvicorn --factory` an open stream holds shutdown for up to its lifetime. `serve` (what
`insightforge-agent serve` runs) tells the Runtime on the stop signal, and streams end at once."""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from insightforge_agent.domain.models import RunState
from insightforge_agent.scheduler.clarification import JOB_TABLE, job_id
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.api.support import VAGUE

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = sys.platform == "win32"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


COMMANDS = {
    "serve": lambda port: ["-m", "tests.api.uvicorn_app", "--port", str(port)],
    "plain uvicorn": lambda port: ["-m", "uvicorn", "tests.api.uvicorn_app:app", "--factory",
                                   "--host", "127.0.0.1", "--port", str(port)],
}


class Server:
    def __init__(self, workdir: Path, how: str, **env_overrides: str) -> None:
        self.port = free_port()
        self.output: list[str] = []
        env = {**os.environ, "T10_SMOKE_DIR": str(workdir), "PYTHONUNBUFFERED": "1",
               **env_overrides}
        self.proc = subprocess.Popen(
            [sys.executable, *COMMANDS[how](self.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
        threading.Thread(target=self._drain, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self.output.append(line.rstrip())

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, "\n".join(self.output)
            try:
                if httpx.get(f"{self.url}/personas", timeout=1).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        raise AssertionError("uvicorn did not start:\n" + "\n".join(self.output))

    def stop(self) -> None:
        """What Ctrl+C (Ctrl+Break on Windows) or a service manager's SIGTERM does."""
        self.proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGTERM)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


@pytest.fixture
def start(tmp_path):
    made: list[Server] = []

    def make(how: str, **env) -> Server:
        made.append(Server(tmp_path, how, **env))
        return made[-1]
    yield make
    for s in made:
        s.kill()


def stop_with_a_stream_open(server: Server, tmp_path) -> tuple[float, str, str]:
    """Start a Run that waits for its answer, follow it over SSE, then send the stop signal.
    Returns how long the process took to exit, its log and the Run ID; checks it exited
    cleanly and released and kept everything it should."""
    server.wait_ready()
    client = httpx.Client(base_url=server.url, timeout=10)
    assert client.post("/session", json={"persona_id": "alice"}).status_code == 200
    run_id = client.post("/research/run", json={"brief": VAGUE}).json()["run_id"]
    deadline = time.monotonic() + 60
    while client.get(f"/research/{run_id}/clarification").status_code != 200:
        assert time.monotonic() < deadline, "\n".join(server.output)
        time.sleep(0.1)

    # An SSE stream on a Run that will not finish by itself (it awaits its answer).
    received: list[str] = []
    streaming = threading.Event()
    ended: dict = {}

    def follow() -> None:
        try:
            with client.stream("GET", "/research/status", params={"run_id": run_id},
                               timeout=httpx.Timeout(10, read=None)) as r:
                for line in r.iter_lines():
                    received.append(line)
                    if line.startswith("id: "):
                        streaming.set()
        except httpx.HTTPError as e:
            ended["error"] = type(e).__name__
        ended["at"] = time.monotonic()

    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    assert streaming.wait(30)

    signalled = time.monotonic()
    server.stop()
    exit_code = server.proc.wait(90)
    took = time.monotonic() - signalled
    follower.join(10)
    log = "\n".join(server.output)

    # uvicorn shuts down gracefully, then re-raises the signal it caught, so the process ends
    # with that signal's status (Ctrl+Break on Windows: 3; SIGTERM elsewhere: -15).
    assert exit_code == (3 if WINDOWS else -signal.SIGTERM), log
    assert not follower.is_alive() and "error" not in ended  # the stream closed cleanly
    assert "event: end" not in received  # not over: the client reconnects with Last-Event-ID

    # The ordered shutdown ran to the end inside the process: the scheduler stopped, then the
    # Runtime closed its resources (that line is logged after every closer ran), then uvicorn
    # finished the lifespan.
    order = [log.index(line) for line in (
        "Scheduler has been shut down",
        "runtime stopped: scheduler and executor shut down, resources closed",
        "Application shutdown complete")]
    assert order == sorted(order), log

    # Nothing durable was lost: the Run still waits for its answer, and its timeout job is
    # still in the job store for the next process.
    db = sqlite3.connect(tmp_path / "app.db")
    try:
        assert [r[0] for r in db.execute(f"SELECT id FROM {JOB_TABLE}")] == [
            job_id("alice", run_id)]
    finally:
        db.close()
    repos = SqliteRepos(str(tmp_path / "app.db"))
    try:
        assert repos.runs.get("alice", run_id).state == RunState.AWAITING_CLARIFICATION
    finally:
        repos.close()
    return took, log, run_id


def test_serve_shuts_down_promptly_with_an_sse_stream_open_and_cleans_up(start, tmp_path):
    took, log, _ = stop_with_a_stream_open(start("serve"), tmp_path)
    assert took < 10, log  # the stream ended on the signal, not at its 60 s lifetime


def test_plain_uvicorn_waits_for_an_open_stream_at_most_its_lifetime(start, tmp_path):
    took, log, _ = stop_with_a_stream_open(
        start("plain uvicorn", SSE_MAX_STREAM_SECONDS="4"), tmp_path)
    assert 3 <= took < 30, log  # bounded by the lifetime: no hang, but not prompt
