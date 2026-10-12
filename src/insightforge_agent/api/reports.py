"""`/reports`: list and retrieve the selected User's persisted Reports (design.md section 9).

Reports are never deleted, so there is no delete endpoint. Another User's Report is 404, like
one that does not exist. Export, PDF and share links are T15."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.models import Report
from insightforge_agent.domain.repositories import ReportRepository


class ReportListItem(BaseModel):
    id: str
    run_id: str
    created_at: datetime
    title: str
    brief: str


class ReportOut(BaseModel):
    id: str
    run_id: str
    created_at: datetime
    body: dict[str, Any]


def _title(report: Report) -> str:
    return str((report.body.get("draft") or {}).get("title") or "")


def reports_router(reports: ReportRepository,
                   current_user_id: Callable[..., str]) -> APIRouter:
    router = APIRouter(prefix="/reports")

    @router.get("", response_model=list[ReportListItem])
    def list_reports(user_id: str = Depends(current_user_id)):
        """Newest first."""
        return [ReportListItem(id=r.id, run_id=r.run_id, created_at=r.created_at,
                               title=_title(r), brief=str(r.body.get("brief", "")))
                for r in reversed(reports.list_for_owner(user_id))]

    @router.get("/{report_id}", response_model=ReportOut)
    def get_report(report_id: str, user_id: str = Depends(current_user_id)):
        try:
            r = reports.get(user_id, report_id)
        except NotFoundError:
            raise HTTPException(404, f"report {report_id} not found") from None
        return ReportOut(id=r.id, run_id=r.run_id, created_at=r.created_at, body=r.body)

    return router
