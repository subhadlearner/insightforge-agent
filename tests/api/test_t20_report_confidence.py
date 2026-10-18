"""T20 B1: section Confidence, the Executive Summary heading and inline Source metadata, in the
Report API, the Markdown export and the public (shared) projection (S11.1, S11.4, S11.7)."""

from insightforge_agent.domain.contracts import (
    Claim,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportSection,
    VerifiedReport,
)
from insightforge_agent.domain.models import Report
from tests.api.support import App, T0
from tests.api.test_t15_delivery import FakePdf, add_report


def with_background_section(app, report: Report) -> Report:
    """A copy of `report` with a second section of Historical background only."""
    body = dict(report.body)
    verified = VerifiedReport.model_validate(body["verified"])
    historical = HistoricalContext.model_validate(body["historical"])
    old = historical.items[0]
    extra = HistoricalItem(**{**old.model_dump(), "id": "hist_outlook", "heading": "Outlook",
                              "statement": "Earlier outlook."})
    section = ReportSection(heading="Outlook", claims=[
        Claim(text="Earlier outlook.", evidence_id="hist_outlook", historical=True)])
    draft = verified.draft.model_copy(update={"sections": [*verified.draft.sections, section]})
    body["verified"] = verified.model_copy(update={"draft": draft}).model_dump(mode="json")
    body["draft"] = draft.model_dump(mode="json")
    body["historical"] = HistoricalContext(items=[*historical.items, extra]).model_dump(
        mode="json")
    copy = Report(id="rep_two", owner_id=report.owner_id, run_id="run_two", created_at=T0,
                  body=body)
    app.runtime.repos.reports.add(copy)
    return copy


def test_the_report_carries_each_sections_confidence_derived_on_read(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = with_background_section(app, add_report(app))
        app.login("alice")
        out = app.client.get(f"/reports/{report.id}").json()
        assert out["section_confidence"] == ["HIGH", "BACKGROUND"]
        assert "section_confidence" not in out["body"]  # derived, never stored


def test_a_malformed_body_has_no_confidence_rather_than_an_error(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        app.runtime.repos.reports.add(Report(id="rep_odd", owner_id="alice", run_id="run_odd",
                                             created_at=T0, body={"brief": "x"}))
        app.login("alice")
        response = app.client.get("/reports/rep_odd")
        assert response.status_code == 200 and response.json()["section_confidence"] == []


def test_markdown_shows_the_summary_heading_confidence_and_inline_sources(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = with_background_section(app, add_report(app))
        app.login("alice")
        text = app.client.get(f"/reports/{report.id}/export.md").text
        lines = text.splitlines()
        assert lines[:5] == ["# EV pricing", "", "## Executive Summary", "", "Verified summary"]
        pricing = lines.index("## Pricing")
        assert lines[pricing + 2] == "*Confidence: HIGH*"
        outlook = lines.index("## Outlook")
        assert lines[outlook + 2] == ("*Background from earlier Reports only; no current "
                                      "Confidence*")
        assert ("- Model 3 costs 29,000 dollars. [Source: example.test, 2030-02-28, "
                "credibility: 0.80]") in lines
        # A document has no domain and here no date or score: said so, never guessed.
        assert ("- Background from an earlier Report of 2030-03-01: Older private context. "
                "[UNVERIFIED] [Source: private.pdf, undated, credibility: not scored]") in lines
        assert "CONFIDENTIAL PDF EXCERPT" not in text  # metadata only, never Passage text


def test_the_shared_projection_carries_confidence_and_no_passage_text(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = with_background_section(app, add_report(app))
        app.login("alice")
        path = app.client.post(f"/reports/{report.id}/shares").json()["path"]
        public = app.client.get(path).json()
        assert [(s["heading"], s["confidence"]) for s in public["sections"]] == [
            ("Pricing", "HIGH"), ("Outlook", "BACKGROUND")]
        assert "CONFIDENTIAL PDF EXCERPT" not in str(public)


def test_a_section_whose_background_does_not_resolve_is_unresolved_not_background(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = with_background_section(app, add_report(app))
        body = dict(report.body)
        body["historical"] = {"items": body["historical"]["items"][:1]}  # hist_outlook gone
        app.runtime.repos.reports.add(Report(id="rep_three", owner_id="alice",
                                             run_id="run_three", created_at=T0, body=body))
        app.login("alice")
        out = app.client.get("/reports/rep_three").json()
        assert out["section_confidence"] == ["HIGH", None]
