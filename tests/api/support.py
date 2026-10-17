"""The real FastAPI app over SQLite files in `tmp_path`, with scripted models and fixture
search (no keys, no network). The Planner answers from the latest instruction in its own
thread, so any number of Runs can interleave on it."""

import copy
import itertools
import json
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from insightforge_agent.api.app import Runtime, create_app
from insightforge_agent.api.session import COOKIE_NAME
from insightforge_agent.config import Settings
from insightforge_agent.domain.models import TERMINAL_STATES, Run, RunState
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.stores.source_store import SourceStore
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, ai, call

USERS_YML = Path(__file__).resolve().parents[2] / "users.yml"
SECRET = "test-session-secret-0123456789abcdef"
BRIEF = fakes.QUERIES["s1"]
VAGUE = "Tell me about the market."
QUESTION = "Which market, region and time period should the research cover?"
_ids = itertools.count()


def planner_reply(messages) -> AIMessage:
    last = messages[-1]
    if not isinstance(last, HumanMessage):
        return ai(text="Done.")  # after a tool result
    text = str(last.content)
    n = next(_ids)
    if text.startswith("PLAN.") and VAGUE in text:
        return ai(call("ask_clarification", {"question": QUESTION}, f"ask-{n}"))
    if text.startswith("PLAN"):
        args = copy.deepcopy(fakes.PLAN_ARGS)
        if text.startswith("PLAN (clarification timed out)"):
            args["assumption"] = "global EV makers"
        return ai(call("submit_plan", args, f"plan-{n}"))
    if text.startswith("RESEARCH") or text.startswith("REPAIR"):
        todos = call("write_todos", {"todos": [
            {"content": q, "status": "pending"} for q in fakes.QUERIES.values()]}, f"todo-{n}")
        return ai(todos, *[fakes.task(s, f"research-{s}-{n}") for s in fakes.QUERIES])
    return ai(text="Done.")


class Gate:
    """Holds every page fetch (so every Run in RESEARCHING) until opened, and records which
    threads fetched."""

    def __init__(self, search: fakes.FixtureSearch, open_: bool = True) -> None:
        self._search = search
        self._open = threading.Event()
        if open_:
            self._open.set()
        self.fetching = threading.Event()  # set once a fetch is waiting or done
        self.threads: set[str] = set()

    def search(self, query, limit):
        return self._search.search(query, limit)

    def fetch(self, url):
        self.threads.add(threading.current_thread().name)
        self.fetching.set()
        assert self._open.wait(60), "the gate was never opened"
        return self._search.fetch(url)

    def open(self) -> None:
        self._open.set()

    def close(self) -> None:
        """Hold fetches again (the web e2e backend toggles this between tests)."""
        self._open.clear()


def settings(tmp_path, **kw) -> Settings:
    db = (tmp_path / "app.db").as_posix()
    return Settings(llm_provider="fake", search_providers=["fake"], session_secret=SECRET,
                    personas_file=str(USERS_YML), database_url=f"sqlite:///{db}",
                    checkpoint_database_url=f"sqlite:///{(tmp_path / 'ck.db').as_posix()}",
                    sse_poll_seconds=0.02, **kw)


def scripted_deps(gate: Gate, now: Callable[[], datetime] | None = None, **overrides):
    def make(repos, checkpointer, timeouts) -> Deps:
        parts = dict(
            repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
            planner_model=ScriptedChat(reply=planner_reply), writer_model=fakes.writer(),
            light_model=fakes.light(), researcher_model=fakes.researcher(), search=gate,
            fetcher=gate, checkpointer=checkpointer, timeouts=timeouts)
        if now is not None:
            parts["now"] = now
        parts.update(overrides)
        return Deps(**parts)
    return make


class App:
    """One process: a Runtime over the files in `tmp_path`, served by a TestClient."""

    def __init__(self, tmp_path, gate: Gate | None = None, now=None, deps_overrides=None,
                 **settings_kw) -> None:
        self.gate = gate or Gate(fakes.FixtureSearch())
        self.runtime = Runtime.build(settings(tmp_path, **settings_kw),
                                     scripted_deps(self.gate, now, **(deps_overrides or {})))
        self.client = TestClient(create_app(self.runtime))

    def __enter__(self) -> "App":
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.gate.open()  # never leave a Run blocked across shutdown
        self.client.__exit__(*exc)

    def login(self, persona: str) -> TestClient:
        self.client.cookies.clear()
        assert self.client.post("/session", json={"persona_id": persona}).status_code == 200
        return self.client

    def submit(self, brief: str = BRIEF, **body):
        return self.client.post("/research/run", json={"brief": brief, **body})

    def run(self, run_id: str, owner: str = "alice") -> Run:
        return self.runtime.repos.runs.get(owner, run_id)

    def wait(self, run_id: str, until: Callable[[Run], bool] = lambda r: r.state in
             TERMINAL_STATES, owner: str = "alice", timeout: float = 60) -> Run:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.run(run_id, owner)
            if until(run):
                return run
            time.sleep(0.02)
        raise AssertionError(f"run {run_id} is still {self.run(run_id, owner).state}")

    def events(self, run_id: str, owner: str = "alice"):
        return self.runtime.repos.events.read_after(owner, run_id, 0, 100_000)


def wait_until(condition: Callable[[], bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


T0 = datetime(2030, 3, 1, 9, tzinfo=UTC)
TERMINAL = {RunState.COMPLETE, RunState.FAILED}


class LiveStream:
    """One SSE request driven straight through the ASGI app on its own event loop, so a test
    can read a live stream frame by frame and then disconnect (TestClient buffers a response
    until it ends, and cannot disconnect). The app must already be started (lifespan)."""

    def __init__(self, app, run_id: str, cookie: str, last_event_id: str | None = None) -> None:
        self._messages: queue.Queue = queue.Queue()
        self._gone = threading.Event()
        self._buffer = ""
        headers = [(b"cookie", f"{COOKIE_NAME}={cookie}".encode())]
        if last_event_id is not None:
            headers.append((b"last-event-id", last_event_id.encode()))
        self._scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/research/status", "raw_path": b"/research/status",
            "query_string": f"run_id={run_id}".encode(), "headers": headers,
            "client": ("testclient", 1), "server": ("testserver", 80), "root_path": ""}
        self._portal_cm = start_blocking_portal()
        portal = self._portal_cm.__enter__()
        self._done = portal.start_task_soon(self._serve, app)
        start = self._messages.get(timeout=30)
        assert start["type"] == "http.response.start", start
        self.status = start["status"]

    async def _serve(self, app) -> None:
        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            while not self._gone.is_set():
                await anyio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def send(message):
            self._messages.put(message)

        try:
            await app(self._scope, receive, send)
        finally:
            self._messages.put(None)

    def frames(self, limit: int | None = None, timeout: float = 60):
        """Parsed frames ({"id", "event", "data"}), until the stream ends or `limit` are read.
        Comments (keep-alives) are skipped."""
        out = []
        deadline = time.monotonic() + timeout
        while limit is None or len(out) < limit:
            while "\n\n" in self._buffer and (limit is None or len(out) < limit):
                raw, self._buffer = self._buffer.split("\n\n", 1)
                frame = parse_frame(raw)
                if frame:
                    out.append(frame)
            if limit is not None and len(out) >= limit:
                break
            message = self._messages.get(timeout=max(0.1, deadline - time.monotonic()))
            if message is None:
                break
            self._buffer += message.get("body", b"").decode()
        return out

    def close(self) -> None:
        """Disconnect, as a browser closing the page does."""
        self._gone.set()
        self._done.result(30)
        self._portal_cm.__exit__(None, None, None)


def parse_frame(raw: str) -> dict | None:
    frame: dict = {"id": None, "event": "message", "data": ""}
    lines = [line for line in raw.split("\n") if line and not line.startswith(":")]
    if not lines:
        return None
    for line in lines:
        field, _, value = line.partition(": ")
        frame[field] = value
    frame["data"] = json.loads(frame["data"]) if frame["data"] else None
    return frame


def read_stream(client: TestClient, run_id: str, last_event_id: str | None = None):
    """A whole stream through TestClient (which returns only once the stream has closed)."""
    headers = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    response = client.get("/research/status", params={"run_id": run_id}, headers=headers)
    frames = [f for f in map(parse_frame, response.text.split("\n\n")) if f]
    return response, frames
