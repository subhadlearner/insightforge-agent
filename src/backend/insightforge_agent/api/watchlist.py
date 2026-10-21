"""Owner-scoped Watchlist CRUD, with the existing persona dependency."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field, ValidationError

from insightforge_agent.domain.diff import EstablishedFinding
from insightforge_agent.domain.retention import FailureRecord

from insightforge_agent.domain.errors import NotFoundError, SubmissionRejected
from insightforge_agent.domain.watchlist import (
    WatchlistConfig,
    WatchlistConflict,
    WatchlistDashboard,
    WatchlistPatch,
)


class WatchlistDashboardOut(WatchlistDashboard):
    """T11 dashboard plus the durable T13/T12 detail that T15 presents."""

    recent_failures: list[FailureRecord] = Field(default_factory=list)
    unconfirmed_findings: list[EstablishedFinding] = Field(default_factory=list)


def _dashboard_out(service, owner_id: str, dashboard: WatchlistDashboard) -> WatchlistDashboardOut:
    """Project persisted state; never recompute Diff semantics in the API."""
    repos = service.deps.repos
    failures: list[FailureRecord] = []
    with repos.lock:
        for run in dashboard.recent_failed_runs:
            try:
                failures.append(repos.runs.failure(owner_id, run.id))
            except NotFoundError:
                # Legacy FAILED Runs may predate T13's durable failure records. Keep the
                # T11 Run entry visible instead of making the whole dashboard unreadable.
                pass
        unconfirmed: list[EstablishedFinding] = []
        if dashboard.baseline_state == "established":
            _, states = repos.diffs.findings(owner_id, dashboard.item.id)
            unconfirmed = [
                state for _, state in sorted(states.items()) if state.unconfirmed
            ]
    return WatchlistDashboardOut(
        **dashboard.model_dump(),
        recent_failures=failures,
        unconfirmed_findings=unconfirmed,
    )


def watchlist_router(service, current_user_id):
    router = APIRouter(prefix="/watchlist")

    def invoke(fn):
        try:
            return fn()
        except NotFoundError:
            raise HTTPException(404, "watchlist item not found") from None
        except WatchlistConflict as e:
            raise HTTPException(409, str(e)) from None
        except (SubmissionRejected, ValidationError) as e:
            raise HTTPException(422, str(e)) from None

    @router.post("", status_code=201, response_model=WatchlistDashboardOut)
    def create(config: WatchlistConfig, user_id: str = Depends(current_user_id)):
        return _dashboard_out(service, user_id, invoke(lambda: service.create(user_id, config)))

    @router.get("", response_model=list[WatchlistDashboardOut])
    def list_items(user_id: str = Depends(current_user_id)):
        with service.deps.repos.lock:
            return [
                _dashboard_out(service, user_id, service.dashboard(user_id, i.id))
                for i in service.deps.repos.watchlists.list_for_owner(user_id)
            ]

    @router.get("/{item_id}", response_model=WatchlistDashboardOut)
    def get(item_id: str, user_id: str = Depends(current_user_id)):
        dashboard = invoke(lambda: service.dashboard(user_id, item_id))
        return _dashboard_out(service, user_id, dashboard)

    @router.patch("/{item_id}", response_model=WatchlistDashboardOut)
    def update(
        item_id: str,
        patch: WatchlistPatch,
        user_id: str = Depends(current_user_id),
    ):
        dashboard = invoke(lambda: service.update(user_id, item_id, patch))
        return _dashboard_out(service, user_id, dashboard)

    @router.delete("/{item_id}", status_code=204)
    def delete(item_id: str, user_id: str = Depends(current_user_id)):
        invoke(lambda: service.delete(user_id, item_id))
        return Response(status_code=204)

    return router
