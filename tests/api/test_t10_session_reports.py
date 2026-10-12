"""T10 AC2-AC4: persona selection through a signed HttpOnly cookie, the one dependency that
resolves it, and owner-scoped Report list and retrieve (no delete) through the real app."""

import pytest
from fastapi.testclient import TestClient

from insightforge_agent.api.app import Runtime
from insightforge_agent.api.session import COOKIE_NAME, PersonaSession
from insightforge_agent.domain.models import Report, Run, RunState
from insightforge_agent.stores.personas import load_personas
from tests.api.support import T0, USERS_YML, App, settings


@pytest.fixture
def client(tmp_path):
    with App(tmp_path) as app:
        app.client.runtime = app.runtime
        yield app.client


def as_persona(client: TestClient, persona_id: str) -> TestClient:
    client.cookies.clear()
    assert client.post("/session", json={"persona_id": persona_id}).status_code == 200
    return client


def store_report(runtime: Runtime, owner: str, run_id: str, title: str) -> Report:
    runtime.repos.runs.add(Run(id=run_id, owner_id=owner, brief=f"brief {run_id}",
                               state=RunState.COMPLETE, created_at=T0))
    report = Report(id=f"rep_{run_id}", owner_id=owner, run_id=run_id, created_at=T0,
                    body={"brief": f"brief {run_id}", "draft": {"title": title, "sections": []}})
    runtime.repos.reports.add(report)
    return report


# --- AC3: personas and the signed cookie ------------------------------------------------------

def test_personas_are_listed_from_users_yml(client):
    listed = client.get("/personas").json()
    assert [p["id"] for p in listed] == [u.id for u in load_personas(USERS_YML)]
    assert {"id", "display_name", "source_toggles"} == set(listed[0])


def test_selecting_a_persona_sets_a_signed_httponly_cookie_that_resolves_to_it(client):
    response = client.post("/session", json={"persona_id": "bob"})
    assert response.status_code == 200 and response.json()["id"] == "bob"
    header = response.headers["set-cookie"]
    assert header.startswith(f"{COOKIE_NAME}=") and "HttpOnly" in header
    assert "samesite=lax" in header.lower() and "Path=/" in header
    value = client.cookies[COOKIE_NAME]
    assert "bob" not in value.split(".")[1]  # the second part is a signature, not the ID
    assert client.get("/session").json()["id"] == "bob"


def test_an_unknown_persona_cannot_be_selected(client):
    response = client.post("/session", json={"persona_id": "mallory"})
    assert response.status_code == 422
    assert COOKIE_NAME not in response.headers.get("set-cookie", "")
    assert client.get("/session").status_code == 401


@pytest.mark.parametrize("forge", [
    lambda s, v: v[:-2] + ("AA" if not v.endswith("AA") else "BB"),  # signature edited
    lambda s, v: PersonaSession(load_personas(USERS_YML), "x" * 40).sign("alice"),  # other key
    lambda s, v: s.sign("alice").split(".")[0] + "." + v.split(".")[1],  # bob's sig, alice's ID
    lambda s, v: "alice",
    lambda s, v: "",
    lambda s, v: "a.b.c",
    lambda s, v: "!!!.???",
])
def test_a_tampered_or_forged_cookie_is_rejected_and_never_another_persona(client, forge):
    as_persona(client, "bob")
    forged = forge(client.runtime.session, client.cookies[COOKIE_NAME])
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, forged)
    assert client.get("/session").status_code == 401
    assert client.get("/reports").status_code == 401


def test_a_cookie_for_a_persona_no_longer_configured_is_rejected(client):
    gone = client.runtime.session.sign("dave")  # validly signed, but not in users.yml
    client.cookies.set(COOKIE_NAME, gone)
    assert client.get("/session").status_code == 401


def test_owner_scoped_endpoints_need_a_persona(client):
    assert client.get("/reports").status_code == 401
    assert client.get("/reports/rep_x").status_code == 401


def test_leaving_the_session_clears_the_cookie(client):
    as_persona(client, "alice")
    assert client.delete("/session").status_code == 204
    assert client.get("/session").status_code == 401


def test_the_app_refuses_to_start_without_a_long_enough_session_secret(tmp_path):
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Runtime.build(settings(tmp_path).model_copy(update={"session_secret": None}))
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        PersonaSession(load_personas(USERS_YML), "short")


# --- AC2 and AC4: Reports, owner-scoped, list and retrieve only ------------------------------

def test_a_user_lists_and_retrieves_only_their_own_reports(client):
    mine = store_report(client.runtime, "alice", "run_a1", "Alice report")
    store_report(client.runtime, "bob", "run_b1", "Bob report")
    as_persona(client, "alice")
    listed = client.get("/reports").json()
    assert [(r["id"], r["title"], r["run_id"]) for r in listed] == [
        (mine.id, "Alice report", "run_a1")]
    got = client.get(f"/reports/{mine.id}").json()
    assert got["id"] == mine.id and got["body"] == mine.body


def test_another_users_report_and_an_unknown_report_are_the_same_404(client):
    theirs = store_report(client.runtime, "bob", "run_b1", "Bob report")
    as_persona(client, "alice")
    foreign = client.get(f"/reports/{theirs.id}")
    unknown = client.get("/reports/rep_nope")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["detail"].replace(theirs.id, "X") == \
        unknown.json()["detail"].replace("rep_nope", "X")
    as_persona(client, "bob")
    assert client.get(f"/reports/{theirs.id}").status_code == 200


def test_there_is_no_report_delete_endpoint(client):
    report = store_report(client.runtime, "alice", "run_a1", "Alice report")
    as_persona(client, "alice")
    assert client.delete(f"/reports/{report.id}").status_code == 405
    assert client.delete("/reports").status_code == 405
    for route in client.app.routes:
        if getattr(route, "path", "").startswith("/reports"):
            assert "DELETE" not in route.methods
    assert client.get(f"/reports/{report.id}").status_code == 200
