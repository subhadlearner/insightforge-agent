"""`/reports`: list and retrieve the selected User's persisted Reports (design.md section 9).

Reports are never deleted, so there is no delete endpoint. Another User's Report is 404, like
one that does not exist. Export, PDF and share links are T15.

`GET /reports/{report_id}/citations` is the citation drawer's data: one `Citation` per Claim,
in draft order, resolved from the Report's persisted provenance by
`pipeline.citations.report_citations` (a Claim's primary Passage, or for a Historical Claim
the Passage it inherited; never a Fact-Checker re-fetch). Provenance that does not resolve is
500, never a partial or invented list. There is no general Source or Passage endpoint."""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from insightforge_agent.domain.contracts import Citation
from insightforge_agent.domain.errors import NotFoundError, ProvenanceError
from insightforge_agent.domain.models import Report
from insightforge_agent.domain.repositories import ReportRepository
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.stores.source_store import SourceStore

logger = logging.getLogger(__name__)


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


def reports_router(reports: ReportRepository, store: SourceStore,
                   current_user_id: Callable[..., str]) -> APIRouter:
    router = APIRouter(prefix="/reports")

    @router.get("", response_model=list[ReportListItem])
    def list_reports(user_id: str = Depends(current_user_id)):
        """Newest first."""
        return [ReportListItem(id=r.id, run_id=r.run_id, created_at=r.created_at,
                               title=_title(r), brief=str(r.body.get("brief", "")))
                for r in reversed(reports.list_for_owner(user_id))]

    def owned(user_id: str, report_id: str) -> Report:
        try:
            return reports.get(user_id, report_id)
        except NotFoundError:
            raise HTTPException(404, f"report {report_id} not found") from None

    @router.get("/{report_id}", response_model=ReportOut)
    def get_report(report_id: str, user_id: str = Depends(current_user_id)):
        r = owned(user_id, report_id)
        return ReportOut(id=r.id, run_id=r.run_id, created_at=r.created_at, body=r.body)

    @router.get("/{report_id}/citations", response_model=list[Citation])
    def get_citations(report_id: str, user_id: str = Depends(current_user_id)):
        """One Citation per Claim, in draft order."""
        report = owned(user_id, report_id)
        try:
            return report_citations(store, user_id, report)
        except ProvenanceError:
            logger.exception("citations of report %s do not resolve", report_id)
            raise HTTPException(500, f"report {report_id} has inconsistent provenance; its "
                                     "citations cannot be resolved") from None

    return router
