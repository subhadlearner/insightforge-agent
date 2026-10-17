"""`/research`: submit a Brief, and follow a Run's progress (design.md sections 6 and 9).

`POST /research/run` answers 202 as soon as the Run is stored and queued on the RunExecutor;
the pipeline never runs in the request. A Brief that fails the Submission check is 422 and
creates no Run. A full or closing executor is 503 (with Retry-After) and creates no Run, except
in the instant shutdown begins between storing the Run and queueing it: that Run is already
FAILED when the 503 is sent.

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
- A stream lives at most `max_stream_seconds` while the Run is unfinished, and ends at once when
  `closing` is set (the server is stopping); either way it closes without `end`, and
  EventSource reconnects with `Last-Event-ID`.
- An unknown Run and another User's Run are the same 404, checked before the cursor.
  Disconnecting only stops the stream; the Run carries on in the RunExecutor.

`POST /research/estimate` is the runtime estimate shown before submit
(`domain.runtime_estimate`): omitted source toggles inherit the persona's defaults exactly as
submission does, and `document_count` stands for the PDFs (no bytes are sent). A heuristic: it
checks no availability (submission may still be 422), and creates no Run, writes nothing and
takes no RunExecutor capacity."""

import base64
import binascii
import json
import re
import threading
from collections.abc import Callable

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from insightforge_agent.domain.contracts import Brief, effective_source_toggles
from insightforge_agent.domain.documents import MAX_DOCUMENTS, Upload
from insightforge_agent.domain.errors import NotFoundError, SubmissionRejected
from insightforge_agent.domain.models import TERMINAL_STATES, RunEvent, User
from insightforge_agent.domain.plan import SourceType
from insightforge_agent.domain.repositories import RunEventRepository, RunRepository
from insightforge_agent.domain.runtime_estimate import EstimateCoefficients, estimate_runtime
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


class RuntimeEstimateIn(BaseModel):
    # Omitted: the persona's defaults from users.yml, else web only (as for a Run).
    source_toggles: dict[SourceType, bool] | None = None
    document_count: int = Field(default=0, ge=0, le=MAX_DOCUMENTS)  # PDFs to be uploaded


class RuntimeEstimateOut(BaseModel):
    """A heuristic duration for the UI, not a promise that the Run finishes within it."""

    estimated_seconds: int
    base_seconds: int
    document_count: int
    source_types: list[SourceType]  # the enabled ones the estimate counted, in canonical order


_TOGGLES = TypeAdapter(dict[SourceType, bool])


def _invalid(e: ValidationError) -> HTTPException:
    return HTTPException(422, e.errors(include_url=False, include_context=False))


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
                    poll_seconds: float, max_stream_seconds: float,
                    closing: threading.Event, estimates: EstimateCoefficients) -> APIRouter:
    router = APIRouter(prefix="/research")

    @router.post("/run", status_code=202, response_model=RunAccepted)
    def submit(body: RunIn, user: User = Depends(current_user)):
        uploads = _uploads(body.documents)
        try:
            brief = Brief(text=body.brief, document_ids=[u.document_id for u in uploads],
                          source_toggles=effective_source_toggles(body.source_toggles,
                                                                  user.source_toggles))
        except ValidationError as e:
            raise _invalid(e) from None
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

    @router.post("/estimate", response_model=RuntimeEstimateOut)
    def estimate(body: RuntimeEstimateIn, user: User = Depends(current_user)):
        try:  # the persona's defaults are validated as a Brief would validate them
            toggles = _TOGGLES.validate_python(
                effective_source_toggles(body.source_toggles, user.source_toggles))
        except ValidationError as e:
            raise _invalid(e) from None
        result = estimate_runtime(estimates, toggles, body.document_count)
        return RuntimeEstimateOut(estimated_seconds=result.estimated_seconds,
                                  base_seconds=result.base_seconds,
                                  document_count=result.document_count,
                                  source_types=result.source_types)

    @router.get("/status")
    async def status(request: Request, run_id: str = Query(min_length=1),
                     last_event_id: str | None = Header(default=None),
                     user: User = Depends(current_user)):
        try:  # ownership first: another User's Run is 404 whatever the cursor
            await anyio.to_thread.run_sync(runs.get, user.id, run_id)
        except NotFoundError:
            raise HTTPException(404, f"run {run_id} not found") from None
        after = parse_cursor(last_event_id)
        return StreamingResponse(
            _stream(request, user.id, run_id, after), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def _read(owner_id: str, run_id: str, cursor: int) -> list[RunEvent]:
        return await anyio.to_thread.run_sync(events.read_after, owner_id, run_id, cursor, BATCH)

    async def _stream(request: Request, owner_id: str, run_id: str, cursor: int):
        sent = [cursor]

        def frames(batch: list[RunEvent]):
            for event in batch:
                sent[0] = event.seq
                yield event_frame(event)

        idle = 0.0
        ends_at = anyio.current_time() + max_stream_seconds
        while True:
            batch = await _read(owner_id, run_id, sent[0])
            for frame in frames(batch):
                yield frame
            if len(batch) == BATCH:
                continue  # more is stored already
            run = await anyio.to_thread.run_sync(runs.get, owner_id, run_id)
            if run.state in TERMINAL_STATES:
                # Read after the state: every event was stored before it, so this is the rest.
                while rest := await _read(owner_id, run_id, sent[0]):
                    for frame in frames(rest):
                        yield frame
                yield end_frame(run_id, run.state.value)
                return
            if await request.is_disconnected():
                return
            if closing.is_set():
                return  # the server is stopping; the client reconnects with Last-Event-ID
            if anyio.current_time() >= ends_at:
                # A bounded lifetime, so no stream holds up a shutdown (or waits forever on a
                # Run awaiting its answer); EventSource reconnects with Last-Event-ID.
                return
            idle = 0.0 if batch else idle + poll_seconds
            if idle >= KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"
                idle = 0.0
            await anyio.sleep(poll_seconds)

    return router
