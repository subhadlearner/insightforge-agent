"""The FastAPI application and its composition root (design.md sections 9 and 11).

`Runtime.build` opens every resource the process owns and wires it, without starting anything;
`create_app` mounts the routers over it and runs `Runtime.start` and `Runtime.stop` in the
lifespan, so no request is served before startup has finished, and nothing is closed while a
request or a Run may still use it. Run it with
`insightforge-agent serve` (`serve` below), which tells the Runtime the moment uvicorn is asked
to stop, so open SSE streams end at once instead of holding uvicorn's graceful shutdown for up
to their lifetime. Plain `uvicorn insightforge_agent.api.app:app_from_env --factory` also works,
but then shutdown waits up to `SSE_MAX_STREAM_SECONDS` for open streams (measured: 60 s).

Startup (`start`), in order:

1. The scheduler starts **paused**: its persisted job store is open, so jobs can be written
   (APScheduler 3 writes them only once started), but no job fires yet.
2. `ClarificationService.time_out` is installed as the timeout handler, so no job can fire
   into a missing handler.
3. `startup_sweep` runs once, before any new Run can be accepted: in-progress Runs are settled,
   RESUMING ones resumed, expired clarifications timed out and the rest rescheduled. Resumes go
   to the RunExecutor, never run on this thread.
4. The scheduler resumes: due and future timeouts now fire, into the executor.
5. The lifespan yields: requests are served.

Shutdown (`stop`), in order: the scheduler is paused and shut down (a timeout can no longer
create executor work) and the handler removed; the executor refuses new work, settles queued
work (a new Run FAILED, a resume left RESUMING for the next sweep) and waits for running Runs
to finish; only then are Qdrant, the checkpointer and SQLite closed, last opened first."""

import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio
import uvicorn
from apscheduler.schedulers.base import BaseScheduler
from fastapi import FastAPI
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.api.clarify import clarification_router
from insightforge_agent.api.reports import reports_router
from insightforge_agent.api.research import research_router
from insightforge_agent.api.session import PersonaSession
from insightforge_agent.config import Settings
from insightforge_agent.pipeline.clarification import ClarificationService
from insightforge_agent.pipeline.deps import Deps, TimeoutScheduler
from insightforge_agent.pipeline.executor import BoundedRunExecutor
from insightforge_agent.pipeline.recovery import startup_sweep
from insightforge_agent.pipeline.research import ResearchService
from insightforge_agent.pipeline.wiring import build_deps, build_qdrant
from insightforge_agent.scheduler.clarification import (
    ApsTimeoutScheduler,
    build_scheduler,
    install_timeout_handler,
)
from insightforge_agent.stores.personas import load_personas
from insightforge_agent.stores.sqlite import SqliteRepos, sqlite_path

logger = logging.getLogger(__name__)

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
    scheduler: BaseScheduler
    executor: BoundedRunExecutor
    clarifications: ClarificationService
    research: ResearchService
    recovered: Counter = field(default_factory=Counter)  # what the startup sweep did
    closing: threading.Event = field(default_factory=threading.Event)  # streams end when set
    _closers: list[Callable[[], object]] = field(default_factory=list)

    @classmethod
    def build(cls, settings: Settings, make_deps: MakeDeps | None = None) -> "Runtime":
        """Open the process's resources and wire them. Nothing runs until `start`.
        `make_deps` replaces the real models, search and Qdrant (tests pass scripted ones)."""
        if settings.session_secret is None:
            raise ValueError("SESSION_SECRET is required to sign the persona cookie")
        session = PersonaSession(load_personas(settings.personas_file),
                                 settings.session_secret.get_secret_value(),
                                 secure=settings.session_cookie_secure)
        closers: list[Callable[[], object]] = []
        try:
            return cls._wire(settings, session, closers, make_deps)
        except BaseException:
            for close in reversed(closers):  # nothing half-built stays open
                close()
            raise

    @classmethod
    def _wire(cls, settings: Settings, session: PersonaSession, closers: list,
              make_deps: MakeDeps | None) -> "Runtime":
        repos = SqliteRepos(sqlite_path(settings.database_url))
        closers.append(repos.close)
        ck = sqlite3.connect(sqlite_path(settings.checkpoint_database_url),
                             check_same_thread=False)
        closers.append(ck.close)
        scheduler = build_scheduler(settings.database_url)
        deps = (make_deps or _production_deps(settings, closers))(
            repos, SqliteSaver(ck), ApsTimeoutScheduler(scheduler))
        executor = BoundedRunExecutor(settings.run_concurrency, settings.run_queue_limit)
        return cls(settings=settings, session=session, repos=repos, deps=deps,
                   scheduler=scheduler, executor=executor,
                   clarifications=ClarificationService(deps, submit=executor.submit),
                   research=ResearchService(deps, executor), _closers=closers)

    def start(self) -> None:
        self.scheduler.start(paused=True)
        install_timeout_handler(self.clarifications.time_out)
        self.recovered = startup_sweep(self.deps, self.clarifications)
        logger.info("startup sweep: %s", dict(self.recovered))
        self.scheduler.resume()

    def begin_shutdown(self) -> None:
        """The server was asked to stop: open progress streams end now (clients reconnect
        later with Last-Event-ID), so they do not hold up the graceful shutdown."""
        self.closing.set()

    def stop(self) -> None:
        self.begin_shutdown()
        if self.scheduler.running:
            self.scheduler.pause()
            self.scheduler.shutdown(wait=True)
        install_timeout_handler(None)
        self.executor.shutdown()
        for close in reversed(self._closers):
            close()
        logger.info("runtime stopped: scheduler and executor shut down, resources closed")


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Both block (the sweep; waiting for running Runs), so keep them off the event loop.
        try:
            await anyio.to_thread.run_sync(runtime.start)
            yield
        finally:
            await anyio.to_thread.run_sync(runtime.stop)

    app = FastAPI(title="InsightForge", lifespan=lifespan)
    app.state.runtime = runtime
    user = runtime.session.current_user()
    user_id = runtime.session.current_user_id()
    app.include_router(runtime.session.router())
    app.include_router(research_router(runtime.research, user, runtime.repos.runs,
                                       runtime.repos.events, runtime.settings.sse_poll_seconds,
                                       runtime.settings.sse_max_stream_seconds,
                                       runtime.closing))
    app.include_router(clarification_router(runtime.clarifications, user_id))
    app.include_router(reports_router(runtime.repos.reports, user_id))
    return app


class _Server(uvicorn.Server):
    """uvicorn, telling the Runtime as soon as a stop signal arrives (before uvicorn starts
    waiting for open connections to close)."""

    def __init__(self, config: uvicorn.Config, runtime: Runtime) -> None:
        super().__init__(config)
        self._runtime = runtime

    def handle_exit(self, sig, frame) -> None:
        self._runtime.begin_shutdown()
        super().handle_exit(sig, frame)


def serve(runtime: Runtime, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the app until SIGINT or SIGTERM (Ctrl+C or Ctrl+Break on Windows)."""
    _Server(uvicorn.Config(create_app(runtime), host=host, port=port), runtime).run()


def app_from_env() -> FastAPI:
    """The uvicorn factory: Settings from the environment and `.env`."""
    return create_app(Runtime.build(Settings()))
