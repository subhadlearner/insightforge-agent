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
