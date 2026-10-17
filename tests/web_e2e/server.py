"""A deterministic FastAPI backend for the web/ Playwright tests (T14). Not production code.

The real app (`create_app` over `Runtime`) on throwaway SQLite files, with the scripted
models and recorded search fixtures of `tests/api/support.py`: no keys, no network, no paid
calls. A Vague Brief (`support.VAGUE`) makes the Planner ask its clarifying question.

A small `/__test__` router lets a browser test steer what it cannot reach through the UI:

- `POST /__test__/gate/close` and `/gate/open`: hold every page fetch, so Runs wait in
  RESEARCHING (to observe a live stream, reconnects and refreshes) until opened.
- `POST /__test__/clock/advance?minutes=N` and `/clock/reset`: move the backend clock (real
  time plus an offset), e.g. past a clarification deadline, so an answer is late (409).
- `POST /__test__/clarification/{run_id}/timeout?owner=alice`: run the durable timeout job now,
  as APScheduler would at the deadline.

SSE streams live `E2E_SSE_SECONDS` (default 3) while a Run is unfinished, so EventSource
reconnects with Last-Event-ID during an ordinary test.

    uv run python -m tests.web_e2e.server   # E2E_PORT (default 8100)
"""

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import APIRouter

from insightforge_agent.api.app import Runtime, create_app
from tests.api.support import Gate, scripted_deps, settings
from tests.pipeline import fakes


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


class ClosableGate(Gate):
    def close(self) -> None:
        self._open.clear()


def build(tmp: Path, sse_seconds: float):
    gate, clock = ClosableGate(fakes.FixtureSearch()), Clock()
    runtime = Runtime.build(settings(tmp, sse_max_stream_seconds=sse_seconds),
                            scripted_deps(gate, clock))
    app = create_app(runtime)
    control = APIRouter(prefix="/__test__", include_in_schema=False)

    @control.post("/gate/close", status_code=204)
    def close_gate():
        gate.close()

    @control.post("/gate/open", status_code=204)
    def open_gate():
        gate.open()

    @control.post("/clock/advance", status_code=204)
    def advance(minutes: float):
        clock.offset += timedelta(minutes=minutes)

    @control.post("/clock/reset", status_code=204)
    def reset_clock():
        clock.offset = timedelta()

    @control.post("/clarification/{run_id}/timeout")
    def time_out(run_id: str, owner: str = "alice"):
        return {"timed_out": runtime.clarifications.time_out(owner, run_id)}

    app.include_router(control)
    return app


def main() -> None:
    port = int(os.environ.get("E2E_PORT", "8100"))
    sse_seconds = float(os.environ.get("E2E_SSE_SECONDS", "3"))
    with tempfile.TemporaryDirectory(prefix="insightforge-e2e-") as tmp:
        app = build(Path(tmp), sse_seconds)
        # Playwright stops the process on exit; open streams end within E2E_SSE_SECONDS.
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning",
                    timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
