"""T15 owner-scoped HTTP projection of the persisted T12 Diff and Alert records.

The API does not recompute Findings, materiality, temporal ordering or Alert wording. Those
semantics belong to T12; these routes only expose the durable records to the UI.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from insightforge_agent.domain.alerts import Alert, DiffRecord
from insightforge_agent.domain.errors import NotFoundError


class AlertReadPatch(BaseModel):
    read: bool


def insights_router(repos, current_user_id) -> APIRouter:
    router = APIRouter()

    def owned_item(owner_id: str, item_id: str) -> None:
        try:
            repos.watchlists.get(owner_id, item_id)
        except NotFoundError:
            raise HTTPException(404, "watchlist item not found") from None

    @router.get("/watchlist/{item_id}/diffs", response_model=list[DiffRecord])
    def list_diffs(item_id: str, user_id: str = Depends(current_user_id)):
        owned_item(user_id, item_id)
        return repos.diffs.diffs(user_id, item_id)

    @router.get("/watchlist/{item_id}/diffs/{run_id}", response_model=DiffRecord)
    def get_diff(item_id: str, run_id: str, user_id: str = Depends(current_user_id)):
        owned_item(user_id, item_id)
        record = repos.diffs.diff(user_id, item_id, run_id)
        if record is None:
            raise HTTPException(404, "diff not found")
        return record

    @router.get("/alerts", response_model=list[Alert])
    def list_alerts(
        item_id: str | None = Query(default=None),
        user_id: str = Depends(current_user_id),
    ):
        if item_id is not None:
            owned_item(user_id, item_id)
        return repos.diffs.alerts(user_id, item_id)

    def owned_alert(owner_id: str, alert_id: str) -> Alert:
        try:
            return repos.diffs.alert(owner_id, alert_id)
        except NotFoundError:
            raise HTTPException(404, "alert not found") from None

    @router.get("/alerts/{alert_id}", response_model=Alert)
    def get_alert(alert_id: str, user_id: str = Depends(current_user_id)):
        return owned_alert(user_id, alert_id)

    @router.patch("/alerts/{alert_id}", response_model=Alert)
    def mark_alert(
        alert_id: str,
        body: AlertReadPatch,
        user_id: str = Depends(current_user_id),
    ):
        owned_alert(user_id, alert_id)
        repos.diffs.mark_read(user_id, alert_id, body.read)
        return owned_alert(user_id, alert_id)

    return router
