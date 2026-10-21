"""Resolving a Run's one clarifying question (design.md section 2, Clarification).

The answer endpoint and the timeout job both go through `ClarificationService._resolve`: one
compare-and-set on the Run moves it from AWAITING_CLARIFICATION to RESUMING and stores the
resolution in the same write. The first resolver wins; the other gets ClarificationConflict
(409 for the endpoint, a no-op for the job). Cancelling the timeout job after an answer is only
clean-up: the compare-and-set alone decides the winner. Resuming happens after the resolution
is stored, through `submit` (the app's RunExecutor), so a request or the scheduler thread only
hands the Run on and never runs the pipeline itself."""

from collections.abc import Callable
from datetime import datetime

from insightforge_agent.domain.errors import ClarificationConflict, NotFoundError
from insightforge_agent.domain.models import Clarification, Run, RunState
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.executor import WorkNotAccepted
from insightforge_agent.pipeline.graph import resume_run

MAX_ANSWER_CHARS = 2000

# Runs a Run's work, as RunExecutor.submit(work, on_cancel=...) does. It may raise
# WorkNotAccepted, and calls `on_cancel` if it drops the work before starting it.
Submit = Callable[..., object]


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

    def reconcile_overdue(self) -> int:
        """Time out every Run that still awaits an answer after its stored deadline, whether or
        not its timeout job exists (scheduling it may have failed). Each goes through
        `time_out`, so the compare-and-set still decides between it, the job and an answer.
        Returns how many this call timed out. No model call is made here."""
        now = self._deps.now()
        timed_out = 0
        overdue = [run for run in self._deps.repos.runs.list_unfinished()
                   if run.state == RunState.AWAITING_CLARIFICATION
                   and run.clarification is not None and run.clarification.expired(now)]
        for run in overdue:
            if self.time_out(run.owner_id, run.id):
                timed_out += 1
        return timed_out

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
        `claimed` is for the startup sweep re-resuming a Run whose claim a crash interrupted.

        The resolution is already stored, so the Run is never failed here. If the executor
        will not take the work (it is shutting down), or cancels it before it starts, the Run
        stays durably RESUMING (or, for `claimed`, PLANNING still waiting at its question),
        which the next startup sweep resumes; `resume_deferred` records why."""
        def deferred(reason: str) -> None:
            self._deps.log(owner_id, run_id, "resume_deferred", reason=reason,
                           recovery="the startup sweep resumes the Run")

        try:
            self._submit(lambda: resume_run(self._deps, owner_id, run_id, claimed=claimed),
                         on_cancel=lambda: deferred("the process shut down before resuming"))
        except WorkNotAccepted as e:
            deferred(f"the executor did not accept the resume: {e}")
