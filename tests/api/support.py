"""The real FastAPI app over SQLite files in `tmp_path`, with scripted models and fixture
search (no keys, no network). The Planner answers from the latest instruction in its own
thread, so any number of Runs can interleave on it."""

import copy
import itertools
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from insightforge_agent.api.app import Runtime, create_app
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
