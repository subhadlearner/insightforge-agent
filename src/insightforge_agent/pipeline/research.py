"""Submitting a Brief for research (design.md sections 2 and 11): validate, durably create the
Run, hand it to the RunExecutor, and return at once with the Run.

The order is what keeps every Run accounted for:

1. `check_brief`: a rejected Brief raises SubmissionRejected and nothing is written.
2. The executor reserves a place before the Run is created: a full or closing executor raises
   ExecutorFull or ExecutorClosed and no Run exists.
3. `start_run` stores the Run; the executor then queues `carry_run`, which ends it COMPLETE,
   FAILED or AWAITING_CLARIFICATION.
4. A created Run whose work is cancelled before it starts (the process is shutting down) is
   settled FAILED with the reason in its log, never left in its first state."""

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload
from insightforge_agent.domain.models import Run
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.executor import RunExecutor
from insightforge_agent.pipeline.graph import (
    StartedRun,
    carry_run,
    check_brief,
    settle_interrupted,
    start_run,
)

NOT_STARTED = "the process shut down before the Run started"


class ResearchService:
    def __init__(self, deps: Deps, executor: RunExecutor) -> None:
        self._deps = deps
        self._executor = executor

    def submit(self, owner_id: str, brief: Brief, uploads: list[Upload] | None = None,
               watchlist_item_id: str | None = None) -> Run:
        """The new Run as stored, before any of it has run. Raises SubmissionRejected (no Run),
        or the executor's WorkNotAccepted (no Run, except a Run created just as shutdown began,
        which is then already FAILED)."""
        deps = self._deps
        checked = check_brief(deps, owner_id, brief, uploads, watchlist_item_id)
        started = self._executor.admit(lambda: start_run(deps, checked),
                                       lambda s: carry_run(deps, s),
                                       self._not_started)
        return started.run

    def _not_started(self, started: StartedRun) -> None:
        settle_interrupted(self._deps, started.run.owner_id, started.run.id, NOT_STARTED)
