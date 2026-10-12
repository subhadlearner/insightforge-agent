"""`/research`: submit a Brief, and follow a Run's progress (design.md sections 6 and 9).

`POST /research/run` answers 202 as soon as the Run is stored and queued on the RunExecutor;
the pipeline never runs in the request. A Brief that fails the Submission check is 422 and
creates no Run. A full or closing executor is 503 (with Retry-After) and creates no Run.

`GET /research/status?run_id=...` is a Server-Sent Events stream of the Run's persisted
`run_events`, tailed with a cursor; nothing is buffered beyond one bounded batch.

- Each event is `id: <seq>` and `data: {"seq", "type", "payload", "created_at"}` (the default
  `message` event). `seq` is the persisted cursor, increasing within a Run (not contiguous: the
  sequence is shared by all Runs).
- `Last-Event-ID: N` (sent by EventSource on reconnect) replays exactly the events with
  seq > N, in order. Absent or empty means from the start. Anything but ASCII digits is 400,
  never a silently different cursor.
- The pipeline stores every event of a Run before the Run becomes terminal, so once the stream
  sees COMPLETE or FAILED it drains what is left, sends `event: end` with the final state, and
  closes. A client that connects after the Run ended gets the whole stored history, then `end`.
- An unknown Run and another User's Run are the same 404, before anything is streamed.
  Disconnecting only stops the stream; the Run carries on in the RunExecutor."""

import base64
import binascii
import json
import re
from collections.abc import Callable

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload
from insightforge_agent.domain.errors import NotFoundError, SubmissionRejected
from insightforge_agent.domain.models import TERMINAL_STATES, RunEvent, User
from insightforge_agent.domain.plan import SourceType
from insightforge_agent.domain.repositories import RunEventRepository, RunRepository
from insightforge_agent.pipeline.executor import ExecutorFull, WorkNotAccepted
from insightforge_agent.pipeline.research import ResearchService

RETRY_AFTER_SECONDS = 10
BATCH = 100  # events read per query: the most the stream ever holds in memory
KEEPALIVE_SECONDS = 15.0
_CURSOR = re.compile(r"[0-9]{1,18}")


class DocumentIn(BaseModel):
    filename: str = Field(min_length=1)
    content_base64: str


class RunIn(BaseModel):
    brief: str = Field(min_length=1)
    # Omitted: the persona's defaults from users.yml, else web only.
    source_toggles: dict[SourceType, bool] | None = None
    documents: list[DocumentIn] = Field(default_factory=list)


class RunAccepted(BaseModel):
    run_id: str
    state: str
    status_url: str


def _uploads(documents: list[DocumentIn]) -> list[Upload]:
    try:
        return [Upload(filename=d.filename, data=base64.b64decode(d.content_base64,
                                                                  validate=True))
                for d in documents]
    except (binascii.Error, ValueError):
        raise HTTPException(422, "a document's content_base64 is not valid base64") from None


def parse_cursor(last_event_id: str | None) -> int:
    """The seq to stream after: 0 for an absent or empty header, else its exact value."""
    if last_event_id is None or last_event_id == "":
        return 0
    if not _CURSOR.fullmatch(last_event_id):
        raise HTTPException(400, "Last-Event-ID must be the decimal id of an event")
    return int(last_event_id)


def event_frame(event: RunEvent) -> str:
    data = json.dumps({"seq": event.seq, "type": event.type, "payload": event.payload,
                       "created_at": event.created_at.isoformat()})
    return f"id: {event.seq}\ndata: {data}\n\n"


def end_frame(run_id: str, state: str) -> str:
    return f"event: end\ndata: {json.dumps({'run_id': run_id, 'state': state})}\n\n"


def research_router(service: ResearchService, current_user: Callable[..., User],
                    runs: RunRepository, events: RunEventRepository,
                    poll_seconds: float) -> APIRouter:
    router = APIRouter(prefix="/research")

    @router.post("/run", status_code=202, response_model=RunAccepted)
    def submit(body: RunIn, user: User = Depends(current_user)):
        uploads = _uploads(body.documents)
        toggles = body.source_toggles if body.source_toggles is not None else (
            user.source_toggles or None)
        try:
            brief = Brief(text=body.brief, document_ids=[u.document_id for u in uploads],
                          **({"source_toggles": toggles} if toggles is not None else {}))
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        try:
            run = service.submit(user.id, brief, uploads)
        except SubmissionRejected as e:
            raise HTTPException(422, str(e)) from None
        except WorkNotAccepted as e:
            why = "at capacity" if isinstance(e, ExecutorFull) else "shutting down"
            raise HTTPException(503, f"research is {why}: {e}",
                                headers={"Retry-After": str(RETRY_AFTER_SECONDS)}) from None
        return RunAccepted(run_id=run.id, state=run.state.value,
                           status_url=f"/research/status?run_id={run.id}")

    @router.get("/status")
    async def status(request: Request, run_id: str = Query(min_length=1),
                     last_event_id: str | None = Header(default=None),
                     user: User = Depends(current_user)):
        after = parse_cursor(last_event_id)
        try:
            await anyio.to_thread.run_sync(runs.get, user.id, run_id)
        except NotFoundError:
            raise HTTPException(404, f"run {run_id} not found") from None
        return StreamingResponse(
            _stream(request, user.id, run_id, after), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def _read(owner_id: str, run_id: str, cursor: int) -> list[RunEvent]:
        return await anyio.to_thread.run_sync(events.read_after, owner_id, run_id, cursor, BATCH)

    async def _stream(request: Request, owner_id: str, run_id: str, cursor: int):
        idle = 0.0
        while True:
            batch = await _read(owner_id, run_id, cursor)
            for event in batch:
                yield event_frame(event)
                cursor = event.seq
            if len(batch) == BATCH:
                continue  # more is stored already
            run = await anyio.to_thread.run_sync(runs.get, owner_id, run_id)
            if run.state in TERMINAL_STATES:
                # Read after the state: every event was stored before it, so this is the rest.
                while rest := await _read(owner_id, run_id, cursor):
                    for event in rest:
                        yield event_frame(event)
                        cursor = event.seq
                yield end_frame(run_id, run.state.value)
                return
            if await request.is_disconnected():
                return
            idle = 0.0 if batch else idle + poll_seconds
            if idle >= KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"
                idle = 0.0
            await anyio.sleep(poll_seconds)

    return router
