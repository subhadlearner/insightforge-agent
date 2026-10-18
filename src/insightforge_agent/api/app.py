"""The FastAPI application and its composition root (design.md sections 9 and 11).

`Runtime.build` opens every resource the process owns and wires it, without starting anything;
`create_app` mounts the routers over it and runs `Runtime.start` and `Runtime.stop` in the
lifespan, so no request is served before startup has finished, and nothing is closed while a
request or a Run may still use it. Run it with
`insightforge-agent serve` (`serve` below), which tells the Runtime the moment uvicorn is asked
to stop, so open SSE streams end at once instead of holding uvicorn's graceful shutdown for up
to their lifetime. Plain `uvicorn insightforge_agent.api.app:app_from_env --factory` also works,
but then shutdown waits up to `SSE_MAX_STREAM_SECONDS` for open streams (60.25 s observed with
the default).

Startup (`start`), in order:

1. The scheduler starts **paused**: its persisted job store is open, so jobs can be written
   (APScheduler 3 writes them only once started), but no job fires yet.
2. `ClarificationService.time_out` is installed as the timeout handler, so no job can fire
   into a missing handler.
3. `startup_sweep` runs once, before any new Run can be accepted: in-progress Runs are settled,
   RESUMING ones resumed, expired clarifications timed out and the rest rescheduled. Resumes go
   to the RunExecutor, never run on this thread.
4. The daily retention job and the clarification reconciliation job are added unless the job
   store already holds them, and the scheduler resumes: due and future timeouts now fire, into
   the executor; a retention sweep missed while the process was down fires once.
5. The lifespan yields: requests are served.

Shutdown (T20 B4):

1. `begin_shutdown` (as soon as the server is asked to stop): open streams end, and the
   executor admits nothing more, so neither `POST /research/run` nor a Watchlist tick creates
   a Run from then on (a Run created at that instant is settled FAILED by `admit`).
2. `stop`: the scheduler is paused, so no job fires (jobs written meanwhile, such as a running
   Run's clarification timeout, are still stored); the executor settles queued work (a new Run
   FAILED, a resume left RESUMING for the next sweep) and waits for running Runs, with every
   resource still open. A Diff such a Run would start is not scheduled; the next startup
   recovers it.
3. The job store is removed from the scheduler, which waits for a job-processing pass that
   began before the pause to finish, and only then is the scheduler shut down (waiting for jobs
   it already started). Shutting it down during such a pass made the pass's removal of a fired
   one-shot job raise JobLookupError on the scheduler thread (SHUT-1). The stored jobs stay in
   the database.
4. The handlers are removed; Qdrant, the checkpointer and SQLite are closed, last opened first.
"""

import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import FrameType
from typing import Any

import anyio
import uvicorn
from apscheduler.schedulers.base import BaseScheduler
from fastapi import FastAPI
from langgraph.checkpoint.sqlite import SqliteSaver

from insightforge_agent.api.clarify import clarification_router
from insightforge_agent.api.delivery import delivery_router
from insightforge_agent.api.insights import insights_router
from insightforge_agent.api.reports import reports_router
from insightforge_agent.api.research import research_router
from insightforge_agent.api.session import PersonaSession
from insightforge_agent.api.watchlist import watchlist_router
from insightforge_agent.pipeline.watchlist import WatchlistService
from insightforge_agent.pipeline.report_delivery import (
    PdfRenderer,
    ShareTokenService,
    SubprocessPlaywrightRenderer,
)
from insightforge_agent.config import Settings
from insightforge_agent.domain.runtime_estimate import EstimateCoefficients
from insightforge_agent.pipeline.clarification import ClarificationService
from insightforge_agent.pipeline.deps import Deps, TimeoutScheduler
from insightforge_agent.pipeline.executor import BoundedRunExecutor
from insightforge_agent.pipeline.recovery import startup_sweep
from insightforge_agent.pipeline.research import ResearchService
from insightforge_agent.pipeline.wiring import build_deps, build_qdrant
from insightforge_agent.scheduler.diff import DiffService, install_diff_handler
from insightforge_agent.scheduler.retention import (
    RetentionSweeper,
    install_retention_handler,
    schedule_retention,
)
from insightforge_agent.scheduler.watchlist import WatchlistScheduler, install_watchlist_handler
from insightforge_agent.scheduler.webhook import WebhookSender, install_webhook_handler
from insightforge_agent.scheduler.clarification import (
    ApsTimeoutScheduler,
    build_scheduler,
    install_reconcile_handler,
    install_timeout_handler,
    schedule_reconciliation,
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


def _estimate_coefficients(settings: Settings) -> EstimateCoefficients:
    return EstimateCoefficients(
        base_seconds=settings.runtime_estimate_base_seconds,
        pdf_ingest_seconds=settings.runtime_estimate_pdf_ingest_seconds,
        source_seconds={"web": settings.runtime_estimate_web_seconds,
                        "documents": settings.runtime_estimate_documents_seconds,
                        "memory": settings.runtime_estimate_memory_seconds})


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
    watchlists: WatchlistService
    retention: RetentionSweeper
    pdf_renderer: PdfRenderer
    share_tokens: ShareTokenService
    recovered: Counter = field(default_factory=Counter)  # what the startup sweep did
    # Set once, when the process is stopping: open streams end. A Runtime is not restarted.
    closing: threading.Event = field(default_factory=threading.Event)
    _closers: list[Callable[[], object]] = field(default_factory=list)

    @classmethod
    def build(cls, settings: Settings, make_deps: MakeDeps | None = None, pdf_renderer: PdfRenderer | None = None) -> "Runtime":
        """Open the process's resources and wire them. Nothing runs until `start`.
        `make_deps` replaces the real models, search and Qdrant (tests pass scripted ones)."""
        if settings.session_secret is None:
            raise ValueError("SESSION_SECRET is required to sign the persona cookie")
        session = PersonaSession(load_personas(settings.personas_file),
                                 settings.session_secret.get_secret_value(),
                                 secure=settings.session_cookie_secure)
        closers: list[Callable[[], object]] = []
        try:
            return cls._wire(settings, session, closers, make_deps, pdf_renderer)
        except BaseException:
            for close in reversed(closers):  # nothing half-built stays open
                close()
            raise

    @classmethod
    def _wire(cls, settings: Settings, session: PersonaSession, closers: list,
              make_deps: MakeDeps | None, pdf_renderer: PdfRenderer | None) -> "Runtime":
        repos = SqliteRepos(sqlite_path(settings.database_url))
        closers.append(repos.close)
        ck = sqlite3.connect(sqlite_path(settings.checkpoint_database_url),
                             check_same_thread=False)
        closers.append(ck.close)
        scheduler = build_scheduler(settings.database_url)
        deps = (make_deps or _production_deps(settings, closers))(
            repos, SqliteSaver(ck), ApsTimeoutScheduler(scheduler))
        executor = BoundedRunExecutor(settings.run_concurrency, settings.run_queue_limit)
        research = ResearchService(deps, executor)
        closing = threading.Event()
        webhooks = WebhookSender(deps, scheduler, closing)
        webhook_urls = {u.id: u.webhook_url for u in session.personas}
        diffs = DiffService(deps, scheduler, closing, webhooks=webhooks,
                            webhook_url_for=webhook_urls.get,
                            unconfirmed_after=settings.not_reconfirmed_threshold)
        schedules = WatchlistScheduler(deps, scheduler, research, closing, diffs=diffs)
        deps.on_complete = schedules.completed
        retention = RetentionSweeper(repos.retention, deps.doc_index, deps.now,
                                     retention_days=settings.retention_days, closing=closing)
        configured_share_secret = (
            settings.share_jwt_secret.get_secret_value()
            if settings.share_jwt_secret is not None else ""
        )
        share_secret = configured_share_secret or settings.session_secret.get_secret_value()
        return cls(settings=settings, session=session, repos=repos, deps=deps,
                   scheduler=scheduler, executor=executor,
                   clarifications=ClarificationService(deps, submit=executor.submit),
                   research=research, watchlists=WatchlistService(deps, schedules),
                   retention=retention,
                   pdf_renderer=pdf_renderer or SubprocessPlaywrightRenderer(),
                   share_tokens=ShareTokenService(share_secret),
                   closing=closing, _closers=closers)

    def start(self) -> None:
        self.scheduler.start(paused=True)
        install_timeout_handler(self.clarifications.time_out)
        install_reconcile_handler(self.clarifications.reconcile_overdue)
        install_watchlist_handler(
            self.watchlists.schedules.tick, self.watchlists.schedules.retry_baseline
        )
        schedules = self.watchlists.schedules
        install_diff_handler(schedules.diffs.run_item)
        install_webhook_handler(schedules.diffs.webhooks.deliver)
        install_retention_handler(self.retention.sweep)
        self.recovered = startup_sweep(self.deps, self.clarifications)
        logger.info("startup sweep: %s", dict(self.recovered))
        schedules.recover()  # also schedules each item's Diff job (seed first, then in order)
        schedules.diffs.webhooks.recover()
        # APScheduler runs on the wall clock; the sweep's cutoff uses `deps.now`.
        schedule_retention(self.scheduler, datetime.now(UTC))  # kept if already stored
        schedule_reconciliation(self.scheduler, datetime.now(UTC))  # likewise
        self.scheduler.resume()

    def begin_shutdown(self) -> None:
        """The server was asked to stop: open progress streams end now (clients reconnect
        later with Last-Event-ID), so they do not hold up the graceful shutdown, and no new
        research is admitted, from a request or a Watchlist tick."""
        self.closing.set()
        self.executor.close()

    def stop(self) -> None:
        self.begin_shutdown()
        running = self.scheduler.running
        if running:
            self.scheduler.pause()
        self.executor.shutdown()  # waits for running Runs; resources stay open meanwhile
        if running:
            # Waits for a job-processing pass begun before the pause (it holds the job stores'
            # lock), so the scheduler is never marked stopped during one (module docstring).
            self.scheduler.remove_jobstore("default")
            self.scheduler.shutdown(wait=True)
        install_timeout_handler(None)
        install_reconcile_handler(None)
        install_watchlist_handler(None)
        install_diff_handler(None)
        install_webhook_handler(None)
        install_retention_handler(None)
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
                                       runtime.closing,
                                       _estimate_coefficients(runtime.settings)))
    app.include_router(clarification_router(runtime.clarifications, user_id))
    app.include_router(watchlist_router(runtime.watchlists, user_id))
    app.include_router(insights_router(runtime.repos, user_id))
    app.include_router(reports_router(runtime.repos.reports, runtime.deps.store,
                                      user_id))
    app.include_router(delivery_router(
        runtime.repos.reports,
        runtime.deps.store,
        runtime.repos.shares,
        user_id,
        runtime.share_tokens,
        runtime.pdf_renderer,
        runtime.session.sign,
        runtime.deps.now,
        runtime.deps.new_id,
        runtime.settings.web_origin,
        runtime.settings.share_link_ttl_days,
    ))
    return app


class _Server(uvicorn.Server):
    """uvicorn, telling the Runtime as soon as a stop signal arrives (before uvicorn starts
    waiting for open connections to close)."""

    def __init__(self, config: uvicorn.Config, runtime: Runtime) -> None:
        super().__init__(config)
        self._runtime = runtime

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._runtime.begin_shutdown()
        super().handle_exit(sig, frame)


def serve(runtime: Runtime, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the app until SIGINT or SIGTERM (Ctrl+C or Ctrl+Break on Windows)."""
    _Server(uvicorn.Config(create_app(runtime), host=host, port=port), runtime).run()


def app_from_env() -> FastAPI:
    """The uvicorn factory: Settings from the environment and `.env`."""
    return create_app(Runtime.build(Settings()))
