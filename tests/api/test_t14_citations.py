"""T14 prerequisite (#15): `GET /reports/{report_id}/citations` through the real app.

Each Claim resolves, through `pipeline.citations.report_citations`, to its own primary Passage
and that Passage's Source: current Claims through the Report's Evidence bundle, Historical
Claims through the Passage they inherited, never the Fact-Checker's `matched` Passage. Another
User's Report is the same 404 as an unknown one; broken provenance fails closed with 500. A
Report's citations survive the T13 retention sweep."""

from datetime import UTC, datetime, timedelta

import pytest

from insightforge_agent.domain.contracts import (
    Claim,
    ClaimVerdict,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    HistoricalContext,
    HistoricalItem,
    ReportDraft,
    ReportSection,
    VerifiedReport,
)
from insightforge_agent.domain.models import (
    Observation,
    Passage,
    Report,
    Run,
    RunState,
    RunSummary,
    Source,
)
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.stores.source_store import PassageDraft
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.api.support import App
from tests.stores.t13_world import OLD, T0, item, ref

PUBLISHED = datetime(2029, 11, 5, tzinfo=UTC)


@pytest.fixture
def app(tmp_path):
    with App(tmp_path, now=lambda: T0) as app:
        yield app


def repos(app: App) -> SqliteRepos:
    return app.runtime.repos


def web(app: App, run_id: str, obs_id: str, paragraphs: int = 3, owner: str = "alice",
        credibility: float | None = 0.8, published: datetime | None = PUBLISHED) -> str:
    """A web Source with one Observation whose Passages read `<obs_id> p<i>`."""
    sid = f"src_{obs_id}"
    repos(app).sources.add(Source(id=sid, owner_id=owner, run_id=run_id, kind="web",
                                  locator=f"https://example.com/{sid}", title=f"Title {sid}",
                                  credibility_score=credibility, published_at=published))
    repos(app).passages.add_all(owner, obs_id, [
        Passage(observation_id=obs_id, index=i, text=f"{obs_id} p{i}")
        for i in range(paragraphs)])
    repos(app).observations.add(Observation(id=obs_id, owner_id=owner, source_id=sid,
                                            run_id=run_id, fetched_at=OLD))
    return sid


def complete(app: App, run_id: str, sections: list[list[Claim]], evidence=(), historical=(),
             verdicts=(), owner: str = "alice", at: datetime = OLD) -> Report:
    """A COMPLETE Run and its Report; `sections` lists each draft section's Claims."""
    repos(app).runs.add(Run(id=run_id, owner_id=owner, brief="EV market",
                            state=RunState.COMPLETE, created_at=at))
    draft = ReportDraft(title="R", sections=[ReportSection(heading=f"S{i}", claims=claims)
                                              for i, claims in enumerate(sections)])
    body = {
        "brief": "EV market",
        "draft": draft.model_dump(mode="json"),
        "verified": VerifiedReport(draft=draft, verdicts=list(verdicts)).model_dump(mode="json"),
        "evidence": EvidenceBundle(sections=[EvidenceSection(
            aspect_id="a1", heading="S0", confidence="MEDIUM",
            items=list(evidence))]).model_dump(mode="json"),
        "historical": HistoricalContext(items=list(historical)).model_dump(mode="json"),
    }
    report = Report(id=f"rep_{run_id}", owner_id=owner, run_id=run_id, created_at=at,
                    body=body)
    repos(app).reports.add(report)
    repos(app).summaries.add(RunSummary(owner_id=owner, run_id=run_id, created_at=at,
                                        brief="EV market", memory_indexed=True))
    return report


def claim(e: EvidenceItem | HistoricalItem) -> Claim:
    return Claim(text=e.statement, evidence_id=e.id, historical=isinstance(e, HistoricalItem))


def historical(run_id: str, primary, supporting=()) -> HistoricalItem:
    return HistoricalItem(id=f"hist_{run_id}", subtask_id="m1", aspect_id="a1", heading="S0",
                          statement="Earlier claim.", report_id=f"rep_{run_id}", run_id=run_id,
                          report_date=OLD.date(), primary=primary, supporting=list(supporting))


def citations(app: App, report_id: str, persona: str = "alice"):
    app.login(persona)
    return app.client.get(f"/reports/{report_id}/citations")


# --- resolution ----------------------------------------------------------------------------


def test_a_current_claim_resolves_to_its_primary_passage_and_source(app):
    web(app, "r1", "obs_1", paragraphs=3)
    ev = item("ev1", ref("obs_1", 2), supporting=[ref("obs_1", 2), ref("obs_1", 0)])
    complete(app, "r1", [[claim(ev)]], evidence=[ev])

    response = citations(app, "rep_r1")

    assert response.status_code == 200
    assert response.json() == [{
        "section": 0, "claim": 0, "evidence_id": "ev1", "source_id": "src_obs_1",
        "source_title": "Title src_obs_1", "published_at": "2029-11-05T00:00:00Z",
        "credibility_score": 0.8, "passage": {"observation_id": "obs_1", "index": 2},
        "passage_text": "obs_1 p2", "page": None, "section_heading": None}]


def test_a_document_passage_carries_its_page_and_heading(app):
    obs = app.runtime.deps.store.ingest_document(
        owner_id="alice", run_id="r1", content_hash="h1", filename="deck.pdf",
        chunks=[_draft("Intro", 1, "Overview"), _draft("Revenue grew 12%.", 4, "Results")],
        fetched_at=OLD, credibility_score=0.7)
    ev = item("ev1", ref(obs.id, 1))
    complete(app, "r1", [[claim(ev)]], evidence=[ev])

    (body,) = citations(app, "rep_r1").json()

    assert body["passage_text"] == "Revenue grew 12%." and body["page"] == 4
    assert body["section_heading"] == "Results" and body["source_title"] == "deck.pdf"
    assert body["credibility_score"] == 0.7


def _draft(text, page, heading):
    return PassageDraft(text, page=page, section_heading=heading)


def test_a_historical_claim_resolves_to_its_original_inherited_passage(app):
    web(app, "r_old", "obs_old", paragraphs=3, credibility=0.55,
        published=datetime(2028, 6, 1, tzinfo=UTC))
    old_ev = item("ev_old", ref("obs_old", 0))
    complete(app, "r_old", [[claim(old_ev)]], evidence=[old_ev])
    web(app, "r_new", "obs_new", paragraphs=1)
    new_ev = item("ev_new", ref("obs_new", 0))
    hist = historical("r_old", ref("obs_old", 1), supporting=[ref("obs_old", 2)])
    complete(app, "r_new", [[claim(new_ev), claim(hist)]], evidence=[new_ev],
             historical=[hist], at=OLD + timedelta(days=1))

    current, background = citations(app, "rep_r_new").json()

    assert current["passage_text"] == "obs_new p0"
    assert background["evidence_id"] == "hist_r_old"
    assert background["passage"] == {"observation_id": "obs_old", "index": 1}
    assert background["passage_text"] == "obs_old p1"
    assert background["source_id"] == "src_obs_old"
    assert background["published_at"] == "2028-06-01T00:00:00Z"
    assert background["credibility_score"] == 0.55


def test_every_claim_gets_exactly_one_citation_in_draft_order(app):
    web(app, "r1", "obs_a", paragraphs=2)
    web(app, "r1", "obs_b", paragraphs=2)
    a0, a1, b1 = (item("a0", ref("obs_a", 0)), item("a1", ref("obs_a", 1)),
                  item("b1", ref("obs_b", 1)))
    # b1 is cited twice; each Claim still gets its own citation at its own position.
    report = complete(app, "r1", [[claim(b1), claim(a0)], [claim(a1), claim(b1)]],
                      evidence=[a0, a1, b1])

    body = citations(app, "rep_r1").json()

    assert [(c["section"], c["claim"], c["evidence_id"], c["passage_text"]) for c in body] == [
        (0, 0, "b1", "obs_b p1"), (0, 1, "a0", "obs_a p0"),
        (1, 0, "a1", "obs_a p1"), (1, 1, "b1", "obs_b p1")]
    expected = report_citations(app.runtime.deps.store, "alice", report)
    assert body == [c.model_dump(mode="json") for c in expected]


def test_a_report_without_claims_has_no_citations(app):
    complete(app, "r1", [[]])
    response = citations(app, "rep_r1")
    assert response.status_code == 200 and response.json() == []


def test_citations_ignore_the_fact_checkers_sample_and_matched_passage(app):
    web(app, "r1", "obs_cited", paragraphs=2)
    web(app, "r1", "obs_refetch", paragraphs=3)  # the Fact-Checker's re-fetch
    sampled, unsampled = item("ev1", ref("obs_cited", 0)), item("ev2", ref("obs_cited", 1))
    verdict = ClaimVerdict(section=0, claim=0, evidence_id="ev1", verdict="verified",
                           check="web_refetch", original=ref("obs_cited", 0),
                           matched=ref("obs_refetch", 2), similarity=0.93)
    complete(app, "r1", [[claim(sampled), claim(unsampled)]], evidence=[sampled, unsampled],
             verdicts=[verdict])
    complete(app, "r2", [[claim(sampled), claim(unsampled)]], evidence=[sampled, unsampled])

    with_sample = citations(app, "rep_r1").json()
    without_sample = citations(app, "rep_r2").json()

    assert [c["passage_text"] for c in with_sample] == ["obs_cited p0", "obs_cited p1"]
    assert all(c["passage"]["observation_id"] == "obs_cited" for c in with_sample)
    assert with_sample == without_sample


def test_a_report_written_by_a_real_run_matches_the_resolver(app):
    app.login("alice")
    run_id = app.submit(source_toggles={"web": True}).json()["run_id"]
    assert app.wait(run_id).state == RunState.COMPLETE
    (report,) = repos(app).reports.list_for_owner("alice")

    body = citations(app, report.id).json()

    claims = [c for s in report.body["draft"]["sections"] for c in s["claims"]]
    assert body and len(body) == len(claims)
    assert [c["evidence_id"] for c in body] == [c["evidence_id"] for c in claims]
    assert body == [c.model_dump(mode="json")
                    for c in report_citations(app.runtime.deps.store, "alice", report)]


# --- ownership -----------------------------------------------------------------------------


def test_another_users_report_is_the_same_404_as_an_unknown_one(app):
    web(app, "rb", "obs_b", owner="bob")
    ev = item("evb", ref("obs_b"))
    complete(app, "rb", [[claim(ev)]], evidence=[ev], owner="bob")
    assert citations(app, "rep_rb", "bob").status_code == 200

    foreign = citations(app, "rep_rb", "alice")
    unknown = citations(app, "rep_nope", "alice")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["detail"] == "report rep_rb not found"
    assert unknown.json()["detail"] == "report rep_nope not found"


def test_no_persona_selected_is_401(app):
    app.client.cookies.clear()
    assert app.client.get("/reports/rep_r1/citations").status_code == 401


# --- integrity -----------------------------------------------------------------------------


def _broken(app: App, how: str) -> None:
    web(app, "r1", "obs_1", paragraphs=2)
    ev = item("ev1", ref("obs_1", 1))
    if how == "unknown evidence id":
        complete(app, "r1", [[Claim(text="x", evidence_id="ev_missing")]], evidence=[ev])
    elif how == "unknown historical id":
        complete(app, "r1", [[Claim(text="x", evidence_id="hist_x", historical=True)]],
                 evidence=[ev])
    elif how == "missing passage":
        complete(app, "r1", [[claim(item("ev2", ref("obs_1", 7)))]],
                 evidence=[item("ev2", ref("obs_1", 7))])
    elif how == "missing observation":
        complete(app, "r1", [[claim(item("ev3", ref("obs_gone")))]],
                 evidence=[item("ev3", ref("obs_gone"))])
    elif how == "malformed body":
        repos(app).runs.add(Run(id="r1", owner_id="alice", brief="b",
                                state=RunState.COMPLETE, created_at=OLD))
        repos(app).reports.add(Report(id="rep_r1", owner_id="alice", run_id="r1",
                                      created_at=OLD, body={"draft": {"title": "R",
                                                                      "sections": []}}))


@pytest.mark.parametrize("how", ["unknown evidence id", "unknown historical id",
                                 "missing passage", "missing observation", "malformed body"])
def test_broken_provenance_fails_closed_and_never_fabricates_or_omits(app, how):
    _broken(app, how)
    response = citations(app, "rep_r1")
    assert response.status_code == 500
    assert response.json()["detail"] == (
        "report rep_r1 has inconsistent provenance; its citations cannot be resolved")


def test_the_report_itself_is_still_served_when_its_provenance_is_broken(app):
    _broken(app, "missing passage")
    app.login("alice")
    assert app.client.get("/reports/rep_r1").status_code == 200


# --- retention -----------------------------------------------------------------------------


def test_citations_survive_the_retention_sweep(app):
    web(app, "r_old", "obs_old", paragraphs=3)
    web(app, "r_old", "obs_uncited", paragraphs=2)
    old_ev = item("ev_old", ref("obs_old", 0))
    complete(app, "r_old", [[claim(old_ev)]], evidence=[old_ev])
    web(app, "r_new", "obs_new", paragraphs=2)
    new_ev = item("ev_new", ref("obs_new", 1))
    hist = historical("r_old", ref("obs_old", 2))
    complete(app, "r_new", [[claim(new_ev), claim(hist)]], evidence=[new_ev],
             historical=[hist], at=OLD + timedelta(days=1))
    before = {r: citations(app, r).json() for r in ("rep_r_old", "rep_r_new")}

    done = app.runtime.retention.sweep()

    assert done["passages"] > 0  # the sweep did remove unreferenced material
    assert repos(app).passages.list("alice", "obs_uncited") == []
    after = {r: citations(app, r).json() for r in ("rep_r_old", "rep_r_new")}
    assert after == before
    assert [c["passage_text"] for c in after["rep_r_new"]] == ["obs_new p1", "obs_old p2"]


# --- OpenAPI -------------------------------------------------------------------------------


def test_the_openapi_schema_is_a_typed_citation_list(app):
    spec = app.client.get("/openapi.json").json()
    get = spec["paths"]["/reports/{report_id}/citations"]["get"]
    schema = get["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["type"] == "array" and schema["items"]["$ref"].endswith("/Citation")
    citation = spec["components"]["schemas"]["Citation"]
    assert {"section", "claim", "evidence_id", "source_id", "source_title", "published_at",
            "credibility_score", "passage", "passage_text", "page",
            "section_heading"} == set(citation["properties"])
