"""The startup sweep (design.md section 11): what to do with each Run a stopped process left
unfinished. Run it once at startup, before new Runs are accepted.

| Persisted state                                   | Recovery                                   |
|---------------------------------------------------|--------------------------------------------|
| INGESTING, PLANNING, RESEARCHING, EXTRACTING,     | FAILED (`settle_interrupted`): the design's |
| SYNTHESIZING, WRITING, FACT_CHECKING ("RUNNING")  | "left RUNNING". A Run whose Report is      |
|                                                   | already stored is COMPLETE instead (T8).   |
| RESUMING                                          | resumed from its checkpoint with the       |
|                                                   | stored resolution (`resume_run`, once)     |
| AWAITING_CLARIFICATION, deadline passed           | timed out through the shared CAS           |
| AWAITING_CLARIFICATION, deadline ahead            | left waiting; its timeout job is scheduled |
|                                                   | again (replacing any surviving one)        |
| COMPLETE, FAILED                                  | not read at all                            |

Every step is idempotent, so a second sweep, or a timeout job firing beside it, changes
nothing more: the compare-and-sets decide, and terminal Runs are never listed."""

from collections import Counter

from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.clarification import ClarificationService
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.graph import settle_interrupted


def startup_sweep(deps: Deps, clarifications: ClarificationService) -> Counter:
    """Recover every unfinished Run. Returns how many Runs got each recovery."""
    done: Counter = Counter()
    for run in deps.repos.runs.list_unfinished():
        owner, run_id = run.owner_id, run.id
        if run.state == RunState.AWAITING_CLARIFICATION:
            if deps.now() >= run.clarification.deadline:
                done["timed_out" if clarifications.time_out(owner, run_id) else "skipped"] += 1
            else:
                if deps.timeouts is not None:  # without one, the next sweep times it out
                    deps.timeouts.schedule(owner, run_id, run.clarification.deadline)
                done["still_awaiting"] += 1
        elif run.state == RunState.RESUMING:
            clarifications.resume(owner, run_id)
            done["resumed"] += 1
        else:
            settled = settle_interrupted(
                deps, owner, run_id, f"the process stopped while the Run was {run.state.value}")
            done["failed" if settled.state == RunState.FAILED else "completed"] += 1
    return done
