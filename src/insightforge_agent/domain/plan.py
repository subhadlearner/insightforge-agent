"""Sub-task plan models (design.md §3)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

SourceType = Literal["web", "documents", "memory"]


class Aspect(BaseModel):
    id: str
    name: str


class SubTask(BaseModel):
    id: str
    aspect_id: str
    query: str
    source_type: SourceType
    time_horizon_months: int = Field(gt=0)
    priority: int = Field(ge=1, description="1 is most important")


class SubTaskPlan(BaseModel):
    aspects: list[Aspect]
    sub_tasks: list[SubTask]

    @model_validator(mode="after")
    def _references_resolve(self) -> "SubTaskPlan":
        aspect_ids = {a.id for a in self.aspects}
        unknown = {t.aspect_id for t in self.sub_tasks} - aspect_ids
        if unknown:
            raise ValueError(f"Sub-tasks reference unknown aspects: {sorted(unknown)}")
        return self


MIN_SUBTASKS = 3
MAX_SUBTASKS = 6
MAX_ASPECTS = 6


def plan_violations(plan: SubTaskPlan, allowed: list[SourceType]) -> list[str]:
    """Every way the plan is outside its bounds, as sentences fit to show the Planner."""
    out: list[str] = []
    n = len(plan.sub_tasks)
    if n < MIN_SUBTASKS:
        out.append(f"the plan has {n} Sub-tasks; it needs at least {MIN_SUBTASKS}")
    if n > MAX_SUBTASKS:
        out.append(f"the plan has {n} Sub-tasks; it may have at most {MAX_SUBTASKS}")
    if len(plan.aspects) > MAX_ASPECTS:
        out.append(f"the plan has {len(plan.aspects)} Aspects; it may have at most {MAX_ASPECTS}")
    ids = [t.id for t in plan.sub_tasks]
    if len(set(ids)) != len(ids):
        out.append("Sub-task ids are not unique")
    covered = {t.aspect_id for t in plan.sub_tasks}
    uncovered = [a.name for a in plan.aspects if a.id not in covered]
    if uncovered:
        out.append(f"no Sub-task covers these Aspects: {', '.join(uncovered)}")
    bad = sorted({t.source_type for t in plan.sub_tasks} - set(allowed))
    if bad:
        out.append(f"source types not allowed: {', '.join(bad)} (allowed: {', '.join(allowed)})")
    return out


def trim_plan(plan: SubTaskPlan) -> tuple[SubTaskPlan, list[str]]:
    """Drop Sub-tasks beyond the maximum, lowest priority first (a larger number is lower),
    later ones first on a tie, never the last Sub-task of an Aspect. Returns the plan and the
    dropped ids. A plan that cannot be trimmed this way is returned unchanged."""
    if len(plan.aspects) > MAX_ASPECTS:
        return plan, []
    tasks = list(plan.sub_tasks)
    dropped: list[str] = []
    while len(tasks) > MAX_SUBTASKS:
        per_aspect: dict[str, int] = {}
        for t in tasks:
            per_aspect[t.aspect_id] = per_aspect.get(t.aspect_id, 0) + 1
        removable = [(i, t) for i, t in enumerate(tasks) if per_aspect[t.aspect_id] > 1]
        if not removable:
            return plan, []
        i, victim = max(removable, key=lambda p: (p[1].priority, p[0]))
        dropped.append(victim.id)
        del tasks[i]
    return plan.model_copy(update={"sub_tasks": tasks}), dropped
