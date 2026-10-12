"""`/research`: submit a Brief, and follow a Run's progress (design.md sections 6 and 9).

`POST /research/run` answers 202 as soon as the Run is stored and queued on the RunExecutor;
the pipeline never runs in the request. A Brief that fails the Submission check is 422 and
creates no Run. A full or closing executor is 503 (with Retry-After) and creates no Run."""

import base64
import binascii
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.models import User
from insightforge_agent.domain.plan import SourceType
from insightforge_agent.pipeline.executor import ExecutorFull, WorkNotAccepted
from insightforge_agent.pipeline.research import ResearchService

RETRY_AFTER_SECONDS = 10


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


def research_router(service: ResearchService,
                    current_user: Callable[..., User]) -> APIRouter:
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

    return router
