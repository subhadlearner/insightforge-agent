"""T14 prerequisite (#15): `POST /research/estimate`, the pre-submit runtime estimate, through
the real app. It follows submission's persona-default rule, needs a selected persona, and
never creates a Run, writes state or takes Run capacity."""

import pytest

from insightforge_agent.domain.documents import MAX_DOCUMENTS
from tests.api.support import App


@pytest.fixture
def app(tmp_path):
    with App(tmp_path) as app:
        yield app


def estimate(app: App, **body):
    return app.client.post("/research/estimate", json=body)


def test_the_handover_example_with_explicit_toggles(app):
    app.login("alice")
    response = estimate(app, source_toggles={"web": True, "documents": True, "memory": False},
                        document_count=2)
    assert response.status_code == 200
    assert response.json() == {"estimated_seconds": 135, "base_seconds": 30,
                               "document_count": 2, "source_types": ["web", "documents"]}


@pytest.mark.parametrize("persona, types, seconds", [
    ("alice", ["web", "documents", "memory"], 30 + 45 + 30 + 15),
    ("bob", ["web", "memory"], 30 + 45 + 15),
    ("carol", ["web", "documents"], 30 + 45 + 30),
])
def test_omitted_toggles_inherit_the_personas_defaults(app, persona, types, seconds):
    app.login(persona)
    body = estimate(app).json()
    assert body["source_types"] == types and body["estimated_seconds"] == seconds
    assert body["document_count"] == 0


def test_explicit_toggles_override_the_persona(app):
    app.login("alice")
    body = estimate(app, source_toggles={"memory": True}).json()
    assert body["source_types"] == ["memory"] and body["estimated_seconds"] == 30 + 15


def test_no_persona_selected_is_401(app):
    app.client.cookies.clear()
    assert estimate(app).status_code == 401


@pytest.mark.parametrize("body", [
    {"document_count": -1},
    {"document_count": MAX_DOCUMENTS + 1},
    {"document_count": "many"},
    {"source_toggles": {"intranet": True}},
    {"source_toggles": {"web": "sometimes"}},
])
def test_invalid_input_is_422(app, body):
    app.login("alice")
    assert estimate(app, **body).status_code == 422


def test_the_document_limit_itself_is_accepted(app):
    app.login("alice")
    body = estimate(app, source_toggles={"documents": True}, document_count=MAX_DOCUMENTS)
    assert body.json()["estimated_seconds"] == 30 + MAX_DOCUMENTS * 15 + 30


def test_availability_is_not_checked_here(app):
    """Memory only with no prior Report is refused at submission; the estimate still answers."""
    app.login("alice")
    assert estimate(app, source_toggles={"memory": True}).status_code == 200
    assert app.submit(source_toggles={"memory": True}).status_code == 422


def test_an_estimate_creates_no_run_and_takes_no_capacity(tmp_path):
    # One worker, no queue: had an estimate reserved capacity, the submit below would be 503.
    with App(tmp_path, run_concurrency=1, run_queue_limit=0) as app:
        app.login("alice")
        for _ in range(5):
            assert estimate(app, document_count=3).status_code == 200
        assert app.runtime.repos.runs.list_for_owner("alice") == []
        submitted = app.submit()
        assert submitted.status_code == 202
        app.wait(submitted.json()["run_id"])
        assert len(app.runtime.repos.runs.list_for_owner("alice")) == 1


def test_the_coefficients_come_from_settings(tmp_path):
    with App(tmp_path, runtime_estimate_base_seconds=10,
             runtime_estimate_pdf_ingest_seconds=2, runtime_estimate_web_seconds=3,
             runtime_estimate_documents_seconds=4,
             runtime_estimate_memory_seconds=5) as app:
        app.login("alice")
        body = estimate(app, document_count=3).json()
        assert body["estimated_seconds"] == 10 + 3 * 2 + 3 + 4 + 5
        assert body["base_seconds"] == 10


def test_the_openapi_schema_is_typed(app):
    spec = app.client.get("/openapi.json").json()
    post = spec["paths"]["/research/estimate"]["post"]
    request = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    response = post["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    schemas = spec["components"]["schemas"]
    assert set(schemas[request.rsplit("/", 1)[1]]["properties"]) == {
        "source_toggles", "document_count"}
    assert set(schemas[response.rsplit("/", 1)[1]]["required"]) == {
        "estimated_seconds", "base_seconds", "document_count", "source_types"}
