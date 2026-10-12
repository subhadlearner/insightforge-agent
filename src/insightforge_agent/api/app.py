"""The FastAPI application and its composition root (design.md sections 9 and 11).

`Runtime.build` opens every resource the process owns; `create_app` mounts the routers over it
and runs `Runtime.start` and `Runtime.stop` in the lifespan, so nothing is served before startup
has finished and nothing is closed while the app still serves. Run it with
`uvicorn insightforge_agent.api.app:app_from_env --factory`."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from insightforge_agent.api.reports import reports_router
from insightforge_agent.api.session import PersonaSession
from insightforge_agent.config import Settings
from insightforge_agent.stores.personas import load_personas
from insightforge_agent.stores.sqlite import SqliteRepos, sqlite_path


@dataclass
class Runtime:
    settings: Settings
    session: PersonaSession
    repos: SqliteRepos

    @classmethod
    def build(cls, settings: Settings) -> "Runtime":
        """Open the process's resources. Nothing runs until `start`."""
        if settings.session_secret is None:
            raise ValueError("SESSION_SECRET is required to sign the persona cookie")
        session = PersonaSession(load_personas(settings.personas_file),
                                 settings.session_secret.get_secret_value(),
                                 secure=settings.session_cookie_secure)
        repos = SqliteRepos(sqlite_path(settings.database_url))
        return cls(settings=settings, session=session, repos=repos)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.repos.close()


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
    user_id: Callable[..., str] = runtime.session.current_user_id()
    app.include_router(runtime.session.router())
    app.include_router(reports_router(runtime.repos.reports, user_id))
    return app


def app_from_env() -> FastAPI:
    """The uvicorn factory: Settings from the environment and `.env`."""
    return create_app(Runtime.build(Settings()))
