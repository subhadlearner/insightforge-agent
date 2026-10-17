"""T15 read surfaces: dashboard detail, persisted Diffs and durable Alerts."""

from datetime import timedelta

from insightforge_agent.domain.models import Run, RunState
from tests.api.support import T0, wait_until
from tests.api.test_t11_lifecycle import setup
from tests.api.test_t12_diff_app import (
    MutableSearch,
    complete_tick,
    diff_done,
    make_app,
)
from tests.pipeline.test_t9_clarification import Clock


def test_dashboard_diff_and_alert_reads_are_owner_scoped_and_do_not_recompute(tmp_path):
    clock, search = Clock(T0), MutableSearch()
    app, _ = make_app(tmp_path, clock, search)
    with app:
        item = setup(app, cadence_minutes=15, max_runs=5)
        first = complete_tick(app, clock, item)
        wait_until(
            lambda: app.runtime.repos.watchlists.baseline_state("alice", item.id)
            == "established"
        )

        search.price = "29,000"
        second = complete_tick(app, clock, item)
        wait_until(lambda: diff_done(app, item, second.id))
        [stored_alert] = app.runtime.repos.diffs.alerts("alice")

        listed = app.client.get(f"/watchlist/{item.id}/diffs")
        assert listed.status_code == 200
        [diff] = listed.json()
        assert diff["run_id"] == second.id
        assert diff["entries"][0]["entry"]["finding"]["derived_from"] == "observation_date"
        assert diff["entries"][0]["entry"]["finding"]["provenance"]["report_id"] == diff["report_id"]
        assert diff["entries"][0]["entry"]["outcome"] == "changed"
        assert diff["entries"][0]["material"] is True

        single = app.client.get(f"/watchlist/{item.id}/diffs/{second.id}")
        assert single.status_code == 200 and single.json() == diff

        alerts_before = app.runtime.repos.diffs.alerts("alice")
        alerts = app.client.get("/alerts", params={"item_id": item.id})
        assert alerts.status_code == 200
        [alert] = alerts.json()
        assert alert["id"] == stored_alert.id
        report_of = {r["run_id"]: r["id"] for r in app.client.get("/reports").json()}
        assert alert["report_id"] == report_of[second.id]
        assert alert["changes"][0]["previous_report_id"] == report_of[first.id]
        assert alert["changes"][0]["when"].startswith("Change detected on ")
        assert app.runtime.repos.diffs.alerts("alice") == alerts_before

        marked = app.client.patch(f"/alerts/{stored_alert.id}", json={"read": True})
        assert marked.status_code == 200 and marked.json()["read"] is True
        assert app.client.get(f"/alerts/{stored_alert.id}").json()["read"] is True

        # T15 adds durable FailureRecord detail without removing T11's recent Run list.
        failed = Run(
            id="run_t15_failed",
            owner_id="alice",
            brief=item.brief,
            state=RunState.RESEARCHING,
            created_at=clock() + timedelta(minutes=1),
            watchlist_item_id=item.id,
        )
        app.runtime.repos.runs.add(failed)
        app.runtime.repos.runs.fail(
            "alice",
            failed.id,
            reason="provider unavailable",
            error_codes=["all_search_providers_failed"],
            failed_at=clock() + timedelta(minutes=2),
        )

        # The dashboard presents T12's stored unconfirmed bit; it does not infer it.
        _, states = app.runtime.repos.diffs.findings("alice", item.id)
        key, state = next(iter(states.items()))
        state = state.model_copy(update={"not_reconfirmed": 3, "unconfirmed": True})
        with app.runtime.repos.lock, app.runtime.repos._db:
            app.runtime.repos._db.execute(
                "UPDATE watchlist_findings SET state=? "
                "WHERE owner_id=? AND item_id=? AND finding_key=?",
                (state.model_dump_json(), "alice", item.id, key),
            )

        dashboard = app.client.get(f"/watchlist/{item.id}")
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["failed_runs"] == 1
        assert body["recent_failed_runs"][0]["id"] == failed.id
        assert body["recent_failures"][0]["run_id"] == failed.id
        assert body["recent_failures"][0]["reason"] == "provider unavailable"
        assert body["recent_failures"][0]["stage"] == "RESEARCHING"
        assert body["unconfirmed_findings"][0]["finding"]["object"] == "29,000 dollars"
        assert body["unconfirmed_findings"][0]["not_reconfirmed"] == 3

        app.login("bob")
        assert app.client.get(f"/watchlist/{item.id}/diffs").status_code == 404
        assert app.client.get(
            f"/watchlist/{item.id}/diffs/{second.id}"
        ).status_code == 404
        assert app.client.get("/alerts", params={"item_id": item.id}).status_code == 404
        assert app.client.get(f"/alerts/{stored_alert.id}").status_code == 404
        assert app.client.patch(
            f"/alerts/{stored_alert.id}", json={"read": False}
        ).status_code == 404
        assert app.client.get("/alerts").json() == []
