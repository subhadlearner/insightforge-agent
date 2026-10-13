"""Low-level Run row insertion and parsing shared by ordinary and Watchlist repositories."""

from datetime import datetime

from insightforge_agent.domain.models import Clarification, Run, RunState


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


def run_from_row(row) -> Run:
    """A `SELECT * FROM runs` row."""
    return Run(
        owner_id=row[0],
        id=row[1],
        brief=row[2],
        state=RunState(row[3]),
        created_at=datetime.fromisoformat(row[4]),
        watchlist_item_id=row[5],
        clarification=Clarification.model_validate_json(row[6]) if row[6] else None,
    )
