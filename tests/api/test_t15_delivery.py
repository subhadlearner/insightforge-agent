"""T15 Report Markdown/PDF export and signed, revocable public links."""

import json
from datetime import timedelta

import jwt

from insightforge_agent.domain.contracts import (
    Claim, EvidenceBundle, EvidenceItem, EvidenceSection, HistoricalContext,
    HistoricalItem, PassageRef, ReportDraft, ReportSection, VerifiedReport,
)
from insightforge_agent.domain.models import Report, Run, RunState
from insightforge_agent.pipeline.report_delivery import JWT_ALGORITHM, PdfRenderError
from insightforge_agent.pipeline.fact_check import summarise
from insightforge_agent.stores.source_store import PassageDraft
from tests.api.support import App, T0


class FakePdf:
    def __init__(self, body=b"%PDF-1.7\nT15"):
        self.body = body
        self.calls = []

    def render(self, url, *, cookie_name, cookie_value):
        self.calls.append((url, cookie_name, cookie_value))
        return self.body


class BrokenPdf:
    def render(self, *args, **kwargs):
        raise PdfRenderError("boom")


def add_report(app, owner="alice"):
    repos = app.runtime.repos
    run = Run(id=f"run_{owner}", owner_id=owner, brief="EV pricing",
              state=RunState.COMPLETE, created_at=T0)
    repos.runs.add(run)

    web_obs = app.runtime.deps.store.ingest_web(
        owner_id=owner, run_id=run.id, url="https://example.test/price",
        title="Web price", body="Model 3 costs 29,000 dollars.", fetched_at=T0,
        credibility_score=0.8, published_at=T0 - timedelta(days=1),
    )
    pdf_obs = app.runtime.deps.store.ingest_document(
        owner_id=owner, run_id=run.id, content_hash=f"secret-{owner}",
        filename="private.pdf",
        chunks=[PassageDraft("CONFIDENTIAL PDF EXCERPT", page=7,
                             section_heading="Private")],
        fetched_at=T0,
    )
    web_ref = PassageRef(observation_id=web_obs.id, index=0)
    pdf_ref = PassageRef(observation_id=pdf_obs.id, index=0)
    evidence = [EvidenceItem(
        id="ev_web", subtask_id="s", aspect_id="a", statement="web",
        confidence="HIGH", primary=web_ref, supporting=[web_ref],
    )]
    historical = HistoricalItem(
        id="hist_pdf", subtask_id="m", aspect_id="a", heading="Pricing",
        statement="Older private context.", report_id="rep_old_private",
        run_id="run_old", report_date=T0.date(), primary=pdf_ref,
    )
    draft = ReportDraft(
        title="EV pricing", summary="Verified summary",
        sections=[ReportSection(heading="Pricing", claims=[
            Claim(text="Model 3 costs 29,000 dollars.", evidence_id="ev_web"),
            Claim(text="Older private context. [UNVERIFIED]",
                  evidence_id="hist_pdf", historical=True),
        ])],
        gaps=["No regional tax comparison."],
    )
    body = {
        "brief": "EV pricing",
        "draft": draft.model_dump(mode="json"),
        "verified": VerifiedReport(
            draft=draft,
            summary={"sampled": 2, "verified": 1, "unverified": 1, "unchecked": 0},
        ).model_dump(mode="json"),
        "evidence": EvidenceBundle(sections=[EvidenceSection(
            aspect_id="a", heading="Pricing", confidence="HIGH", items=evidence,
        )]).model_dump(mode="json"),
        "historical": HistoricalContext(items=[historical]).model_dump(mode="json"),
    }
    report = Report(id=f"rep_{owner}", owner_id=owner, run_id=run.id,
                    created_at=T0, body=body)
    repos.reports.add(report)
    return report


def test_markdown_is_verified_deterministic_and_owner_scoped(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = add_report(app)
        app.login("alice")
        first = app.client.get(f"/reports/{report.id}/export.md")
        second = app.client.get(f"/reports/{report.id}/export.md")
        assert first.status_code == 200 and first.content == second.content
        text = first.text
        assert "# EV pricing" in text and "Verified summary" in text
        assert "Older private context. [UNVERIFIED]" in text
        assert "## Gaps & limitations" in text
        assert "Web price" in text and "https://example.test/price" in text
        assert first.headers["content-type"].startswith("text/markdown")
        assert "attachment" in first.headers["content-disposition"]

        add_report(app, "bob")
        assert app.client.get("/reports/rep_bob/export.md").status_code == 404


def test_pdf_scopes_before_render_and_uses_signed_persona_cookie(tmp_path):
    renderer = FakePdf()
    with App(
        tmp_path, now=lambda: T0, pdf_renderer=renderer,
        web_origin="http://web.test:3200",
    ) as app:
        report = add_report(app)
        app.login("alice")
        response = app.client.get(f"/reports/{report.id}/export.pdf")
        assert response.status_code == 200 and response.content.startswith(b"%PDF-")
        [(url, cookie_name, cookie_value)] = renderer.calls
        assert url == f"http://web.test:3200/reports/{report.id}?print=1"
        assert cookie_name == "insightforge_persona"
        assert app.runtime.session.verify(cookie_value).id == "alice"
        assert response.headers["content-type"] == "application/pdf"

        add_report(app, "bob")
        assert app.client.get("/reports/rep_bob/export.pdf").status_code == 404
        assert len(renderer.calls) == 1


def test_pdf_renderer_failure_is_clear(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=BrokenPdf()) as app:
        report = add_report(app)
        app.login("alice")
        response = app.client.get(f"/reports/{report.id}/export.pdf")
        assert response.status_code == 502
        assert "Chromium" in response.json()["detail"]


def test_share_is_public_without_cookie_and_revocation_survives_restart(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = add_report(app)
        app.login("alice")
        created = app.client.post(f"/reports/{report.id}/shares")
        assert created.status_code == 201
        data = created.json()
        token = data["path"].split("/share/", 1)[1]
        assert jwt.get_unverified_header(token)["alg"] == JWT_ALGORITHM

        app.client.cookies.clear()
        public = app.client.get(data["path"])
        assert public.status_code == 200
        body = public.json()
        assert body["id"] == report.id
        assert body["title"] == "EV pricing"
        assert body["fact_check"]["verified"] == 1
        assert len(body["web_sources"]) == 1
        assert body["web_sources"][0]["title"] == "Web price"
        assert body["web_sources"][0]["url"] == "https://example.test/price"
        raw = public.text
        assert "CONFIDENTIAL PDF EXCERPT" not in raw
        assert "private.pdf" not in raw
        assert "run_events" not in raw
        assert "rep_old_private" not in raw

        app.login("alice")
        assert app.client.delete(
            f"/reports/{report.id}/shares/{data['share_id']}"
        ).status_code == 204
        app.client.cookies.clear()
        assert app.client.get(data["path"]).status_code == 404

    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as restarted:
        restarted.client.cookies.clear()
        assert restarted.client.get(data["path"]).status_code == 404


def test_share_tamper_expiry_and_owner_scope_are_refused(tmp_path):
    clock = {"now": T0}
    with App(
        tmp_path, now=lambda: clock["now"], pdf_renderer=FakePdf(),
        share_link_ttl_days=7,
    ) as app:
        report = add_report(app)
        add_report(app, "bob")
        app.login("alice")
        data = app.client.post(f"/reports/{report.id}/shares").json()
        token = data["path"].split("/share/", 1)[1]

        head, payload, signature = token.split(".")
        changed = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered = ".".join((head, payload, changed))
        app.client.cookies.clear()
        assert app.client.get(f"/share/{tampered}").status_code == 404

        clock["now"] = T0 + timedelta(days=7)
        assert app.client.get(data["path"]).status_code == 404

        clock["now"] = T0
        app.login("alice")
        assert app.client.post("/reports/rep_bob/shares").status_code == 404
        assert app.client.delete(
            f"/reports/rep_bob/shares/{data['share_id']}"
        ).status_code == 404


def test_revoking_one_share_does_not_revoke_another(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = add_report(app)
        app.login("alice")
        first = app.client.post(f"/reports/{report.id}/shares").json()
        second = app.client.post(f"/reports/{report.id}/shares").json()
        assert first["share_id"] != second["share_id"]

        assert app.client.delete(
            f"/reports/{report.id}/shares/{first['share_id']}"
        ).status_code == 204

        app.client.cookies.clear()
        assert app.client.get(first["path"]).status_code == 404
        assert app.client.get(second["path"]).status_code == 200


def test_public_fact_check_uses_explicit_allowlist_with_production_summary(tmp_path):
    with App(tmp_path, now=lambda: T0, pdf_renderer=FakePdf()) as app:
        report = add_report(app)
        production_summary = summarise(total=2, verdicts=[], threshold=0.85)
        production_summary.update(sampled=2, verified=1, unverified=1, unchecked=0)
        body = dict(report.body)
        verified = dict(body["verified"])
        verified["summary"] = production_summary | {"private_future_setting": "do not publish"}
        body["verified"] = verified
        with app.runtime.repos.lock, app.runtime.repos._db:
            app.runtime.repos._db.execute(
                "UPDATE reports SET body=? WHERE owner_id=? AND id=?",
                (json.dumps(body), "alice", report.id),
            )
        app.login("alice")
        share = app.client.post(f"/reports/{report.id}/shares")
        assert share.status_code == 201
        app.client.cookies.clear()
        response = app.client.get(share.json()["path"])
        assert response.status_code == 200
        assert response.json()["fact_check"] == {
            "sampled": 2, "verified": 1, "unverified": 1, "unchecked": 0,
        }
        for field in ("implemented", "claims", "similarity_threshold", "pass_rate", "private_future_setting"):
            assert field not in response.text
