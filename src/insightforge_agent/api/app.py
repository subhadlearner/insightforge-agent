"""The FastAPI application and its composition root (design.md sections 9 and 11).

`Runtime.build` opens every resource the process owns; `create_app` mounts the routers over it
and runs `Runtime.start` and `Runtime.stop` in the lifespan, so nothing is served before startup
has finished and nothing is closed while the app still serves. Run it with
`uvicorn insightforge_agent.api.app:app_from_env --factory`."""

import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.api.reports import reports_router
from insightforge_agent.api.research import research_router
from insightforge_agent.api.session import PersonaSession
from insightforge_agent.config import Settings
from insightforge_agent.pipeline.deps import Deps, TimeoutScheduler
from insightforge_agent.pipeline.executor import BoundedRunExecutor
from insightforge_agent.pipeline.research import ResearchService
from insightforge_agent.pipeline.wiring import build_deps, build_qdrant
from insightforge_agent.stores.personas import load_personas
from insightforge_agent.stores.sqlite import SqliteRepos, sqlite_path

# Builds a Run's dependencies over the process's repositories, checkpointer and timeouts.
MakeDeps = Callable[[SqliteRepos, Any, TimeoutScheduler | None], Deps]


def _production_deps(settings: Settings, closers: list) -> MakeDeps:
    def make(repos: SqliteRepos, checkpointer: Any, timeouts: TimeoutScheduler | None) -> Deps:
        qdrant = build_qdrant(settings)
        closers.append(qdrant.close)
        return build_deps(settings, repos, checkpointer, timeouts, qdrant=qdrant)
    return make


@dataclass
class Runtime:
    settings: Settings
    session: PersonaSession
    repos: SqliteRepos
    deps: Deps
    executor: BoundedRunExecutor
    research: ResearchService
    _closers: list[Callable[[], object]] = field(default_factory=list)

    @classmethod
    def build(cls, settings: Settings, make_deps: MakeDeps | None = None) -> "Runtime":
        """Open the process's resources. Nothing runs until `start`. `make_deps` replaces the
        real models, search and Qdrant (tests pass scripted ones)."""
        if settings.session_secret is None:
            raise ValueError("SESSION_SECRET is required to sign the persona cookie")
        session = PersonaSession(load_personas(settings.personas_file),
                                 settings.session_secret.get_secret_value(),
                                 secure=settings.session_cookie_secure)
        closers: list[Callable[[], object]] = []
        repos = SqliteRepos(sqlite_path(settings.database_url))
        closers.append(repos.close)
        ck = sqlite3.connect(sqlite_path(settings.checkpoint_database_url),
                             check_same_thread=False)
        closers.append(ck.close)
        checkpointer = SqliteSaver(ck)
        deps = (make_deps or _production_deps(settings, closers))(repos, checkpointer, None)
        executor = BoundedRunExecutor(settings.run_concurrency, settings.run_queue_limit)
        return cls(settings=settings, session=session, repos=repos, deps=deps,
                   executor=executor, research=ResearchService(deps, executor),
                   _closers=closers)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        """Stop taking work, let running Runs finish (queued ones are settled), then close the
        resources they used, last opened first."""
        self.executor.shutdown()
        for close in reversed(self._closers):
            close()


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="InsightForge", lifespan=lifespan)
    app.state.runtime = runtime
    user = runtime.session.current_user()
    user_id = runtime.session.current_user_id()
    app.include_router(runtime.session.router())
    app.include_router(research_router(runtime.research, user))
    app.include_router(reports_router(runtime.repos.reports, user_id))
    return app


def app_from_env() -> FastAPI:
    """The uvicorn factory: Settings from the environment and `.env`."""
    return create_app(Runtime.build(Settings()))
