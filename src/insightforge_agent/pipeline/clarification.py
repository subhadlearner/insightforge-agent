"""Resolving a Run's one clarifying question (design.md section 2, Clarification).

The answer endpoint and the timeout job both go through `ClarificationService._resolve`: one
compare-and-set on the Run moves it from AWAITING_CLARIFICATION to RESUMING and stores the
resolution in the same write. The first resolver wins; the other gets ClarificationConflict
(409 for the endpoint, a no-op for the job). Cancelling the timeout job after an answer is only
clean-up: the compare-and-set alone decides the winner. Resuming happens after the resolution
is stored, through `submit` (T10's RunExecutor in the app), so a request or the scheduler
thread only hands the Run on and never runs the pipeline itself."""

from collections.abc import Callable
from datetime import datetime

from insightforge_agent.domain.errors import ClarificationConflict, NotFoundError
from insightforge_agent.domain.models import Clarification, Run, RunState
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.graph import resume_run

MAX_ANSWER_CHARS = 2000

Submit = Callable[[Callable[[], object]], object]  # runs a Run's work, e.g. an executor's submit


class ClarificationService:
    def __init__(self, deps: Deps, submit: Submit) -> None:
        self._deps = deps
        self._submit = submit

    def question(self, owner_id: str, run_id: str) -> Clarification:
        """The Run's clarifying question and its status. NotFoundError for a Run that is not
        the User's; ClarificationConflict for one that never asked a question."""
        run = self._deps.repos.runs.get(owner_id, run_id)
        if run.clarification is None:
            raise ClarificationConflict(f"run {run_id} has not asked a clarifying question")
        return run.clarification

    def answer(self, owner_id: str, run_id: str, text: str) -> Run:
        """The User's answer. Accepted only while the Run awaits it and before its deadline;
        otherwise ClarificationConflict (a duplicate or late answer). ValueError for an empty
        or overlong answer, NotFoundError for another User's Run."""
        text = text.strip()
        if not text or len(text) > MAX_ANSWER_CHARS:
            raise ValueError(f"an answer needs 1 to {MAX_ANSWER_CHARS} characters")
        run = self._deps.repos.runs.get(owner_id, run_id)
        now = self._deps.now()
        if run.clarification is not None and run.clarification.expired(now):
            raise ClarificationConflict(f"run {run_id}: the clarification deadline has passed")
        resolved = self._resolve(owner_id, run_id, answer=text, now=now)
        try:
            if self._deps.timeouts is not None:
                self._deps.timeouts.cancel(owner_id, run_id)
        except Exception as e:  # noqa: BLE001 - clean-up only; a late job loses the CAS anyway
            self._deps.log(owner_id, run_id, "clarification_timeout_not_cancelled",
                           error=f"{type(e).__name__}: {e}")
        self.resume(owner_id, run_id)
        return resolved

    def time_out(self, owner_id: str, run_id: str) -> bool:
        """The timeout: resolves the Run as timed out if it still awaits an answer and its
        deadline has passed. False when an answer won first or the Run is gone; a job that
        fired before the deadline is scheduled again at it."""
        try:
            run = self._deps.repos.runs.get(owner_id, run_id)
        except NotFoundError:
            return False
        now = self._deps.now()
        if run.state != RunState.AWAITING_CLARIFICATION or run.clarification is None:
            return False
        if not run.clarification.expired(now):  # fired early: the obligation must not lapse
            self._deps.timeouts.schedule(owner_id, run_id, run.clarification.deadline)
            return False
        try:
            self._resolve(owner_id, run_id, answer=None, now=now)
        except ClarificationConflict:
            return False
        self.resume(owner_id, run_id)
        return True

    def _resolve(self, owner_id: str, run_id: str, *, answer: str | None,
                 now: datetime) -> Run:
        """The one compare-and-set both resolvers share. The resolution is stored in the same
        write that makes the Run RESUMING, so a crash after it is recovered by resuming, never
        by asking again."""
        run = self._deps.repos.runs.resolve_clarification(owner_id, run_id, answer=answer,
                                                          resolved_at=now)
        self._deps.log(owner_id, run_id, "clarification_resolved",
                       resolution=run.clarification.resolution)
        self._deps.log(owner_id, run_id, "stage", state=RunState.RESUMING.value)
        return run

    def resume(self, owner_id: str, run_id: str, *, claimed: bool = False) -> None:
        """Hand a RESUMING Run to `submit`. `resume_run` lets only one caller carry it on;
        `claimed` is for the startup sweep re-resuming a Run whose claim a crash interrupted."""
        self._submit(lambda: resume_run(self._deps, owner_id, run_id, claimed=claimed))
