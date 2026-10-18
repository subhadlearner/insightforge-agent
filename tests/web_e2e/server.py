"""A deterministic FastAPI backend for the web/ Playwright tests (T14). Not production code.

The real app (`create_app` over `Runtime`) on throwaway SQLite files, with the scripted
models and recorded search fixtures of `tests/api/support.py`: no keys, no network, no paid
calls. A Vague Brief (`support.VAGUE`) makes the Planner ask its clarifying question.

A small `/__test__` router lets a browser test steer what it cannot reach through the UI:

- `POST /__test__/gate/close` and `/gate/open`: hold every page fetch, so Runs wait in
  RESEARCHING (to observe a live stream, reconnects and refreshes) until opened.
- `POST /__test__/clock/advance?minutes=N` and `/clock/reset`: move the backend clock (real
  time plus an offset), e.g. past a clarification deadline, so an answer is late (409).
- `POST /__test__/clarification/{run_id}/timeout?owner=alice`: run the durable timeout job now,
  as APScheduler would at the deadline.
- `GET /__test__/capabilities`: whether real uploaded-document research is wired (and if not,
  why), and every outbound connection the process attempted (all blocked).

Uploaded documents are researched with the real Docling, fastembed and in-process Qdrant when
their model files are cached (`tests/web_e2e/real_documents.py`; `E2E_REAL_DOCUMENTS`
`auto` by default, `1` to require them, `0` to skip them). The whole process runs behind
`tests/network_guard.py`: no outbound connection or DNS lookup can succeed.

SSE streams live `E2E_SSE_SECONDS` (default 3) while a Run is unfinished, so EventSource
reconnects with Last-Event-ID during an ordinary test.

    uv run python -m tests.web_e2e.server   # E2E_PORT (default 8100)
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import APIRouter

from insightforge_agent.api.app import Runtime, create_app
from insightforge_agent.domain.alerts import Alert, AlertChange
from insightforge_agent.domain.contracts import (
    Claim, EvidenceBundle, EvidenceItem, EvidenceSection, PassageRef,
    ReportDraft, ReportSection, VerifiedReport,
)
from insightforge_agent.domain.diff import DiffEntry, EstablishedFinding
from insightforge_agent.domain.findings import Finding, FindingProvenance
from insightforge_agent.domain.models import Report, Run, RunState
from insightforge_agent.domain.watchlist import WatchlistItem
from tests.api.support import Gate, scripted_deps, settings
from tests.pipeline import fakes
from tests.web_e2e import real_documents


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset


def build(tmp: Path, sse_seconds: float, blocked: list[str] | None = None):
    gate, clock = Gate(fakes.FixtureSearch()), Clock()
    documents, capabilities = real_documents.load(os.environ.get("E2E_REAL_DOCUMENTS", "auto"))
    runtime = Runtime.build(settings(
        tmp,
        sse_max_stream_seconds=sse_seconds,
        web_origin=os.environ.get("E2E_WEB_ORIGIN", "http://127.0.0.1:3200"),
    ),
                            scripted_deps(gate, clock, **documents))
    app = create_app(runtime)
    control = APIRouter(prefix="/__test__", include_in_schema=False)

    @control.post("/gate/close", status_code=204)
    def close_gate():
        gate.close()

    @control.post("/gate/open", status_code=204)
    def open_gate():
        gate.open()

    @control.post("/clock/advance", status_code=204)
    def advance(minutes: float):
        clock.offset += timedelta(minutes=minutes)

    @control.post("/clock/reset", status_code=204)
    def reset_clock():
        clock.offset = timedelta()

    @control.get("/capabilities")
    def capabilities_now():
        return {**capabilities, "blocked_network": list(blocked or [])}

    @control.post("/clarification/{run_id}/timeout")
    def time_out(run_id: str, owner: str = "alice"):
        return {"timed_out": runtime.clarifications.time_out(owner, run_id)}


    @control.post("/t15/seed")
    def seed_t15():
        """Deterministic persisted T15 state for browser tests; no model/provider calls."""
        repos, owner, item_id = runtime.repos, "alice", "wl_e2e"
        try:
            repos.watchlists.get(owner, item_id)
            return {"item_id": item_id, "report_id": "rep_t15_2"}
        except Exception:
            pass

        now = clock()
        item = WatchlistItem(
            id=item_id, owner_id=owner, brief="Model 3 pricing in China",
            cadence_minutes=15, max_runs=5, source_toggles={"web": True},
            created_at=now, next_tick_at=now + timedelta(days=1),
        )
        repos.watchlists.add(item)

        def complete(run_id: str, value: str, minute: int):
            at = now + timedelta(minutes=minute)
            run = Run(
                id=run_id, owner_id=owner, brief=item.brief, state=RunState.COMPLETE,
                created_at=at, watchlist_item_id=item_id,
            )
            repos.runs.add(run)
            obs = runtime.deps.store.ingest_web(
                owner_id=owner, run_id=run_id,
                url=f"https://example.test/{run_id}", title=f"Pricing source {run_id}",
                body=f"Model 3 costs {value} dollars in China.", fetched_at=at,
                credibility_score=0.8, published_at=at,
            )
            ref = PassageRef(observation_id=obs.id, index=0)
            evidence = EvidenceItem(
                id=f"ev_{run_id}", subtask_id="s1", aspect_id="pricing",
                statement=f"Model 3 costs {value} dollars in China.",
                confidence="HIGH", primary=ref, supporting=[ref],
                entity="Model 3", predicate="starting_price", value=f"{value} dollars",
                scope="China", as_of_period=at.date().isoformat(),
                derived_from="observation_date",
            )
            claim = Claim(text=evidence.statement, evidence_id=evidence.id)
            feature_obs = None
            feature_ref = None
            feature_evidence = None
            feature_claim = None
            if run_id == "t15_1":
                feature_obs = runtime.deps.store.ingest_web(
                    owner_id=owner, run_id=run_id,
                    url="https://example.test/t15_1-feature",
                    title="Feature source t15_1",
                    body="Model 3 does not include Autopilot in China.",
                    fetched_at=at, credibility_score=0.8, published_at=at,
                )
                feature_ref = PassageRef(observation_id=feature_obs.id, index=0)
                feature_evidence = EvidenceItem(
                    id="ev_t15_feature", subtask_id="s1", aspect_id="feature",
                    statement="Model 3 does not include Autopilot in China.",
                    confidence="HIGH", primary=feature_ref, supporting=[feature_ref],
                    entity="Model 3", predicate="feature", value="Autopilot",
                    scope="China", as_of_period=at.date().isoformat(),
                    derived_from="observation_date",
                )
                feature_claim = Claim(text=feature_evidence.statement, evidence_id=feature_evidence.id)
            draft = ReportDraft(
                title=f"Model 3 pricing — {value} dollars",
                summary=f"Observed Model 3 pricing at {value} dollars.",
                sections=[ReportSection(heading="Pricing", claims=[claim])] +
                         ([ReportSection(heading="Feature", claims=[feature_claim])] if feature_claim else []),
                gaps=["Observed price date is derived from the source fetch date."],
            )
            body = {
                "brief": item.brief,
                "draft": draft.model_dump(mode="json"),
                "verified": VerifiedReport(
                    draft=draft,
                    summary={"sampled": 1 + int(feature_claim is not None), "verified": 1 + int(feature_claim is not None), "unverified": 0, "unchecked": 0},
                ).model_dump(mode="json"),
                "evidence": EvidenceBundle(sections=[EvidenceSection(
                    aspect_id="pricing", heading="Pricing", confidence="HIGH",
                    items=[evidence],
                )] + ([EvidenceSection(
                    aspect_id="feature", heading="Feature", confidence="HIGH",
                    items=[feature_evidence],
                )] if feature_evidence else [])).model_dump(mode="json"),
                "historical": {"items": []},
            }
            report = Report(
                id=f"rep_{run_id}", owner_id=owner, run_id=run_id,
                created_at=at, body=body,
            )
            repos.reports.add(report)
            finding = Finding(
                key="model_3|price|china", subject="Model 3", predicate="price",
                object=f"{value} dollars", object_key=f"{value} dollars",
                polarity="affirmed", scope="China", as_of_period=at.date().isoformat(),
                derived_from="observation_date",
                provenance=FindingProvenance(
                    run_id=run_id, report_id=report.id, evidence_id=evidence.id,
                    claim=claim.text, statement=evidence.statement, primary=ref,
                    supporting=[ref], source_ids=[obs.source_id],
                    observation_dates={obs.id: at},
                ),
            )
            feature_finding = None
            if feature_evidence and feature_ref and feature_obs:
                feature_finding = Finding(
                    key="model_3|feature|china", subject="Model 3",
                    predicate="feature", object="Autopilot", object_key="autopilot",
                    polarity="negated", scope="China",
                    as_of_period=at.date().isoformat(), derived_from="observation_date",
                    provenance=FindingProvenance(
                        run_id=run_id, report_id=report.id,
                        evidence_id=feature_evidence.id, claim=feature_claim.text,
                        statement=feature_evidence.statement, primary=feature_ref,
                        supporting=[feature_ref], source_ids=[feature_obs.source_id],
                        observation_dates={feature_obs.id: at},
                    ),
                )
            return run, report, finding, feature_finding

        first, first_report, old, unconfirmed_finding = complete("t15_1", "32,000", 0)
        second, second_report, current, _ = complete("t15_2", "29,000", 15)
        third, third_report, third_price, _ = complete("t15_3", "29,000", 35)
        fourth, fourth_report, fourth_price, _ = complete("t15_4", "29,000", 45)
        assert unconfirmed_finding is not None
        failed = Run(
            id="t15_failed", owner_id=owner, brief=item.brief,
            state=RunState.RESEARCHING, created_at=now + timedelta(minutes=30),
            watchlist_item_id=item_id,
        )
        repos.runs.add(failed)
        repos.runs.fail(
            owner, failed.id, reason="fixture provider unavailable",
            error_codes=["all_search_providers_failed"],
            failed_at=now + timedelta(minutes=31),
        )

        entry = DiffEntry(
            outcome="changed", key=current.key, finding=current, previous=old,
        )
        state = EstablishedFinding(
            finding=fourth_price, not_reconfirmed=0, unconfirmed=False,
        )
        # A separate established negative Finding was not reconfirmed in three successes.
        unconfirmed_state = EstablishedFinding(
            finding=unconfirmed_finding, not_reconfirmed=3, unconfirmed=True,
        )
        alert = Alert(
            id="al_t15_2", owner_id=owner, watchlist_item_id=item_id,
            run_id=second.id, report_id=second_report.id, brief=item.brief,
            created_at=second.created_at,
            changes=[AlertChange(
                outcome="changed", subject=current.subject,
                predicate=current.predicate, scope=current.scope,
                object=current.object, polarity=current.polarity,
                as_of_period=current.as_of_period, derived=True,
                when=f"Change detected on {current.as_of_period}",
                report_id=second_report.id, evidence_id=current.provenance.evidence_id,
                previous_object=old.object, previous_polarity=old.polarity,
                previous_report_id=first_report.id, previous_run_id=first.id,
            )],
        )
        detail = {
            "entries": [
                entry.model_dump(mode="json"),
                DiffEntry(
                    outcome="not_reconfirmed", key=unconfirmed_finding.key,
                    previous=unconfirmed_finding, finding=None,
                    not_reconfirmed=1, unconfirmed=False,
                ).model_dump(mode="json"),
            ],
            "skipped": [],
            "intervening_failed": [],
        }
        with repos.lock, repos._db:
            repos._db.execute(
                "INSERT INTO watchlist_baseline_state VALUES (?,?,?,?,?,?)",
                (owner, item_id, first.id, 2, now.isoformat(), "{}"),
            )
            repos._db.execute(
                "INSERT INTO watchlist_findings VALUES (?,?,?,?)",
                (owner, item_id, current.key, state.model_dump_json()),
            )
            repos._db.execute(
                "INSERT INTO watchlist_findings VALUES (?,?,?,?)",
                (owner, item_id, unconfirmed_finding.key, unconfirmed_state.model_dump_json()),
            )
            repos._db.execute(
                "INSERT INTO watchlist_diffs "
                "(owner_id,item_id,run_id,report_id,status,attempts,error,detail,alert_id,updated_at) "
                "VALUES (?,?,?,?, 'succeeded',1,'',?,?,?)",
                (owner, item_id, second.id, second_report.id, json.dumps(detail),
                 alert.id, second.created_at.isoformat()),
            )
            for earlier, later, report, new_price, misses in (
                (current, third, third_report, third_price, 2),
                (third_price, fourth, fourth_report, fourth_price, 3),
            ):
                continuation = {
                    "entries": [
                        DiffEntry(
                            outcome="reconfirmed", key=new_price.key,
                            finding=new_price, previous=earlier,
                        ).model_dump(mode="json"),
                        DiffEntry(
                            outcome="not_reconfirmed", key=unconfirmed_finding.key,
                            previous=unconfirmed_finding, finding=None,
                            not_reconfirmed=misses, unconfirmed=(misses >= 3),
                        ).model_dump(mode="json"),
                    ],
                    "skipped": [], "intervening_failed": [],
                }
                repos._db.execute(
                    "INSERT INTO watchlist_diffs "
                    "(owner_id,item_id,run_id,report_id,status,attempts,error,detail,alert_id,updated_at) "
                    "VALUES (?,?,?,?, 'succeeded',1,'',?,NULL,?)",
                    (owner, item_id, later.id, report.id,
                     json.dumps(continuation), later.created_at.isoformat()),
                )
            repos._db.execute(
                "INSERT INTO watchlist_alerts "
                "(owner_id,id,item_id,run_id,created_at,read,alert) VALUES (?,?,?,?,?,0,?)",
                (owner, alert.id, item_id, second.id, alert.created_at.isoformat(),
                 alert.model_dump_json()),
            )
        return {"item_id": item_id, "report_id": second_report.id}

    app.include_router(control)
    return app


def main() -> None:
    port = int(os.environ.get("E2E_PORT", "8100"))
    sse_seconds = float(os.environ.get("E2E_SSE_SECONDS", "3"))
    blocked = real_documents.guard_network()  # before anything loads, the models included
    with tempfile.TemporaryDirectory(prefix="insightforge-e2e-") as tmp:
        app = build(Path(tmp), sse_seconds, blocked)
        # Playwright stops the process on exit; open streams end within E2E_SSE_SECONDS.
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning",
                    timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
