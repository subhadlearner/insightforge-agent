"""T15 private export/share operations and token-scoped public Report access."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from insightforge_agent.api.session import COOKIE_NAME
from insightforge_agent.domain.errors import NotFoundError, ProvenanceError
from insightforge_agent.domain.sharing import ShareRecord
from insightforge_agent.pipeline.report_delivery import (
    PdfRenderError,
    PdfRenderer,
    PublicReport,
    ShareTokenError,
    ShareTokenService,
    markdown_export,
    public_projection,
    report_url,
)

logger = logging.getLogger(__name__)


class ShareCreated(BaseModel):
    share_id: str
    path: str
    expires_at: datetime


def delivery_router(
    reports,
    store,
    shares,
    current_user_id,
    tokens: ShareTokenService,
    pdf_renderer: PdfRenderer,
    sign_persona_cookie: Callable[[str], str],
    now: Callable,
    new_id: Callable[[], str],
    web_origin: str,
    share_ttl_days: int,
) -> APIRouter:
    router = APIRouter()

    def owned(owner_id: str, report_id: str):
        try:
            return reports.get(owner_id, report_id)
        except NotFoundError:
            raise HTTPException(404, f"report {report_id} not found") from None

    @router.get("/reports/{report_id}/export.md")
    def export_markdown(report_id: str, user_id: str = Depends(current_user_id)):
        report = owned(user_id, report_id)
        try:
            body = markdown_export(report, store, user_id)
        except ProvenanceError as e:
            logger.exception("report %s Markdown projection failed", report_id)
            raise HTTPException(
                500,
                f"report {report_id} has inconsistent provenance; it cannot be exported",
            ) from e
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report_id}.md"'},
        )

    @router.get("/reports/{report_id}/export.pdf")
    def export_pdf(report_id: str, user_id: str = Depends(current_user_id)):
        owned(user_id, report_id)
        try:
            body = pdf_renderer.render(
                report_url(web_origin, report_id),
                cookie_name=COOKIE_NAME,
                cookie_value=sign_persona_cookie(user_id),
            )
        except PdfRenderError as e:
            logger.exception("PDF export of report %s failed", report_id)
            raise HTTPException(502, "PDF rendering failed; verify Chromium is installed") from e
        return Response(
            content=body,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{report_id}.pdf"'},
        )

    @router.post("/reports/{report_id}/shares", response_model=ShareCreated, status_code=201)
    def create_share(report_id: str, user_id: str = Depends(current_user_id)):
        owned(user_id, report_id)
        created = now()
        record = ShareRecord(
            owner_id=user_id, id=f"share_{new_id()}",
            report_id=report_id, created_at=created,
            expires_at=created + timedelta(days=share_ttl_days),
        )
        shares.add(record)
        token = tokens.issue(record)
        return ShareCreated(
            share_id=record.id, path=f"/share/{token}", expires_at=record.expires_at,
        )

    @router.get("/reports/{report_id}/shares", response_model=list[ShareCreated])
    def active_shares(report_id: str, user_id: str = Depends(current_user_id)):
        owned(user_id, report_id)
        return [
            ShareCreated(
                share_id=record.id,
                path=f"/share/{tokens.issue(record)}",
                expires_at=record.expires_at,
            )
            for record in shares.list_for_report(user_id, report_id, now())
        ]

    @router.delete("/reports/{report_id}/shares/{share_id}", status_code=204)
    def revoke_share(report_id: str, share_id: str, user_id: str = Depends(current_user_id)):
        owned(user_id, report_id)
        try:
            shares.revoke(user_id, report_id, share_id, now())
        except NotFoundError:
            raise HTTPException(404, "share link not found") from None
        return Response(status_code=204)

    @router.get("/share/{token}", response_model=PublicReport)
    def open_share(token: str):
        try:
            claims = tokens.verify(token, now())
            record = shares.get(claims.owner_id, claims.share_id)
            if (
                record.report_id != claims.report_id
                or int(record.expires_at.timestamp()) != int(claims.expires_at.timestamp())
                or record.revoked_at is not None
                or now() >= record.expires_at
            ):
                raise ShareTokenError("inactive share")
            report = reports.get(claims.owner_id, claims.report_id)
            return public_projection(report, store, claims.owner_id)
        except (ShareTokenError, NotFoundError):
            raise HTTPException(404, "shared report not found") from None
        except ProvenanceError as e:
            logger.exception("public share provenance is inconsistent")
            raise HTTPException(404, "shared report not found") from e

    return router
