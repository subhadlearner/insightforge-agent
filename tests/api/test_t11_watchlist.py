import pytest
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.pipeline.graph import run_brief
from tests.api.support import App, BRIEF, VAGUE


def create(app, **kw):
    return app.client.post(
        "/watchlist", json={"brief": BRIEF, "cadence_minutes": 15, **kw}
    )


def test_crud_and_owner_isolation(tmp_path):
    with App(tmp_path) as app:
        app.login("alice")
        response = create(app)
        assert response.status_code == 201, response.text
        item = response.json()["item"]
        iid = item["id"]
        assert response.json()["runs_started"] == 0
        assert len(app.client.get("/watchlist").json()) == 1
        assert app.client.get(f"/watchlist/{iid}").json()["item"] == item
        updated = app.client.patch(
            f"/watchlist/{iid}", json={"cadence_minutes": 1, "max_runs": 2}
        )
        assert updated.status_code == 200
        assert updated.json()["item"]["revision"] == 2
        app.login("bob")
        assert app.client.get("/watchlist").json() == []
        for path in (iid, "missing"):
            assert app.client.get(f"/watchlist/{path}").status_code == 404
            assert (
                app.client.patch(
                    f"/watchlist/{path}", json={"brief": VAGUE}
                ).status_code
                == 404
            )
            assert app.client.delete(f"/watchlist/{path}").status_code == 404
        with pytest.raises(NotFoundError):
            run_brief(app.runtime.deps, "bob", Brief(text=BRIEF), watchlist_item_id=iid)
        assert app.runtime.repos.runs.list_for_owner("bob") == []
        app.login("alice")
        assert app.client.delete(f"/watchlist/{iid}").status_code == 204
        assert app.client.get("/watchlist").json() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"brief": VAGUE},
        {"brief": " "},
        {"source_toggles": {"web": False}},
        {"source_toggles": {"web": True, "documents": True}},
        {"documents": [{"name": "bad.pdf"}]},
        {"document_ids": ["a"]},
        {"cadence_minutes": 0},
        {"cadence_minutes": 1},
        {"max_runs": 0},
        {"max_runs": -1},
        {"max_runs": True},
        {"cadence_minutes": 1.5},
        {"source_toggles": {"web": True, "bogus": True}},
    ],
)
def test_reject_before_storing(tmp_path, changes):
    with App(tmp_path) as app:
        app.login("alice")
        response = create(app, **changes)
        assert response.status_code == 422, response.text
        assert app.runtime.repos.watchlists.list_all() == []
        assert app.runtime.repos.runs.list_for_owner("alice") == []


def test_update_revalidates_without_replacing_on_failure(tmp_path):
    with App(tmp_path) as app:
        app.login("alice")
        item = create(app).json()["item"]
        for changes in (
            {"brief": VAGUE},
            {"source_toggles": {"web": False}},
            {"cadence_minutes": 1},
            {"owner_id": "bob"},
            {"max_runs": None, "cadence_minutes": 1},
        ):
            assert (
                app.client.patch(f"/watchlist/{item['id']}", json=changes).status_code
                == 422
            )
            assert app.client.get(f"/watchlist/{item['id']}").json()["item"] == item


def test_optional_memory_and_short_cadence_with_limit(tmp_path):
    with App(tmp_path) as app:
        app.login("alice")
        response = create(
            app,
            cadence_minutes=1,
            max_runs=1,
            source_toggles={"web": True, "memory": True},
        )
        assert response.status_code == 201
