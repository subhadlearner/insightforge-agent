"""Source dates from run_846d12a50177: a statistics page (roadgenius.com) states
`datePublished: 2024-09-30` and `dateModified: 2026-10-05` (checked on the live page,
2026-10-21). The Run recorded the publication date only, so its Report cited 2025 sales "from" a
page dated 2024-09-30. The publication date stays exactly as the page states it; the page's own
later modification date is recorded beside it and shown, and nothing is ever guessed."""

import json
from datetime import UTC, datetime

import pytest

from insightforge_agent.agents.web import html_to_page
from insightforge_agent.domain.models import Source
from insightforge_agent.pipeline.report_delivery import inline_source
from insightforge_agent.stores.sqlite import SqliteRepos

NOW = datetime(2026, 10, 21, tzinfo=UTC)
PUBLISHED = datetime(2024, 9, 30, tzinfo=UTC)
UPDATED = datetime(2026, 10, 5, tzinfo=UTC)


def page(*dates: tuple[str, str]) -> str:
    ld = {"@type": "Article", **dict(dates)}
    return (f'<html><head><title>T</title><script type="application/ld+json">{json.dumps(ld)}'
            "</script></head><body><p>BYD sold 2,256,714 passenger BEVs in 2025.</p></body></html>")


def test_a_page_updated_after_publication_keeps_both_dates_as_stated():
    p = html_to_page("https://roadgenius.com/x", page(("datePublished", "2024-09-30"),
                                                      ("dateModified", "2026-10-05")), NOW)
    assert (p.published_at, p.updated_at) == (PUBLISHED, UPDATED)


@pytest.mark.parametrize("dates", [
    [("dateModified", "2026-10-05")],  # no publication date: never promoted to one
    [("datePublished", "2024-09-30"), ("dateModified", "2024-09-30")],  # not later
    [("datePublished", "2024-09-30"), ("dateModified", "2020-01-01")],  # earlier: inconsistent
    [("datePublished", "2024-09-30"), ("dateModified", "2027-01-01")],  # after now
], ids=["no-publication-date", "same", "earlier", "future"])
def test_no_update_date_is_recorded_unless_the_page_states_a_later_one(dates):
    p = html_to_page("https://roadgenius.com/x", page(*dates), NOW)
    assert p.updated_at is None
    assert p.published_at == (PUBLISHED if dates[0][0] == "datePublished" else None)


def test_the_citation_shows_the_update_and_the_date_survives_storage(tmp_path):
    source = Source(id="src_1", owner_id="e2e", run_id="run_1", kind="web",
                    locator="https://roadgenius.com/cars/ev/statistics/sales-by-automaker/byd",
                    title="BYD", credibility_score=0.55, published_at=PUBLISHED,
                    updated_at=UPDATED)
    assert inline_source(source) == ("[Source: roadgenius.com, 2024-09-30, updated 2026-10-05, "
                                     "credibility: 0.55]")
    repos = SqliteRepos(str(tmp_path / "run.db"))
    repos.sources.add(source)
    repos.close()
    reopened = SqliteRepos(str(tmp_path / "run.db"))
    assert reopened.sources.get("e2e", "src_1").updated_at == UPDATED
    reopened.close()


def test_a_database_from_before_the_column_opens_and_reads_its_sources_undated_for_updates(
        tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE sources (
        owner_id TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
        locator TEXT NOT NULL, title TEXT NOT NULL, credibility_score REAL, published_at TEXT,
        PRIMARY KEY (owner_id, id));
        INSERT INTO sources VALUES ('e2e','src_1','run_1','web','https://a.example/x','A',0.5,
                                    '2024-09-30T00:00:00+00:00');""")
    con.commit()
    con.close()
    repos = SqliteRepos(str(db))
    old = repos.sources.get("e2e", "src_1")
    repos.close()
    assert (old.published_at, old.updated_at) == (PUBLISHED, None)
