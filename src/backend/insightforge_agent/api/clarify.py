"""The clarification endpoints (design.md sections 2 and 9). T10 mounts this router in the app
and supplies `current_user_id`, its persona-cookie dependency; nothing here knows how a User is
identified. Another User's Run is 404, like a Run that does not exist."""

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from insightforge_agent.domain.errors import ClarificationConflict, NotFoundError
from insightforge_agent.pipeline.clarification import MAX_ANSWER_CHARS, ClarificationService


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)


class ClarificationOut(BaseModel):
    run_id: str
    question: str
    deadline: datetime
    resolution: str | None


class AcceptedOut(BaseModel):
    run_id: str
    state: str


def clarification_router(service: ClarificationService,
                         current_user_id: Callable[..., str]) -> APIRouter:
    router = APIRouter(prefix="/research")

    @router.get("/{run_id}/clarification", response_model=ClarificationOut)
    def get_question(run_id: str, user_id: str = Depends(current_user_id)):
        try:
            c = service.question(user_id, run_id)
        except (NotFoundError, ClarificationConflict) as e:
            raise HTTPException(404, str(e)) from None
        return ClarificationOut(run_id=run_id, question=c.question, deadline=c.deadline,
                                resolution=c.resolution)

    @router.post("/{run_id}/clarify", status_code=202, response_model=AcceptedOut)
    def clarify(run_id: str, body: AnswerIn, user_id: str = Depends(current_user_id)):
        try:
            run = service.answer(user_id, run_id, body.answer)
        except NotFoundError as e:
            raise HTTPException(404, str(e)) from None
        except ClarificationConflict as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return AcceptedOut(run_id=run.id, state=run.state.value)

    return router
