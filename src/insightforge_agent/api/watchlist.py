"""Owner-scoped Watchlist CRUD, with the existing persona dependency."""

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import ValidationError

from insightforge_agent.domain.errors import NotFoundError, SubmissionRejected
from insightforge_agent.domain.watchlist import (
    WatchlistConfig,
    WatchlistConflict,
    WatchlistDashboard,
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

    @router.post("", status_code=201, response_model=WatchlistDashboard)
    def create(config: WatchlistConfig, user_id: str = Depends(current_user_id)):
        return invoke(lambda: service.create(user_id, config))

    @router.get("", response_model=list[WatchlistDashboard])
    def list_items(user_id: str = Depends(current_user_id)):
        with service.deps.repos.lock:
            return [
                service.dashboard(user_id, i.id)
                for i in service.deps.repos.watchlists.list_for_owner(user_id)
            ]

    @router.get("/{item_id}", response_model=WatchlistDashboard)
    def get(item_id: str, user_id: str = Depends(current_user_id)):
        return invoke(lambda: service.dashboard(user_id, item_id))

    @router.patch("/{item_id}", response_model=WatchlistDashboard)
    def update(
        item_id: str, changes: dict = Body(...), user_id: str = Depends(current_user_id)
    ):
        return invoke(lambda: service.update(user_id, item_id, changes))

    @router.delete("/{item_id}", status_code=204)
    def delete(item_id: str, user_id: str = Depends(current_user_id)):
        invoke(lambda: service.delete(user_id, item_id))
        return Response(status_code=204)

    return router
