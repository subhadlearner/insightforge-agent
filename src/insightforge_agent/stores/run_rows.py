"""Low-level Run row insertion shared by ordinary and Watchlist admission."""

from insightforge_agent.domain.models import Run


def insert_run_row(db, run: Run) -> None:
    db.execute(
        "INSERT INTO runs "
        "(owner_id,id,brief,state,created_at,watchlist_item_id,clarification) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            run.owner_id,
            run.id,
            run.brief,
            run.state.value,
            run.created_at.isoformat(),
            run.watchlist_item_id,
            run.clarification.model_dump_json() if run.clarification else None,
        ),
    )
