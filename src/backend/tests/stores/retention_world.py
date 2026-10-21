"""Stored Runs, Reports, Watchlist records and uploaded-document chunks on a SQLite file, with
the T13 retention sweeper over in-memory Qdrant and a settable clock. Records are written
straight to the repositories, so each test controls exactly what refers to what."""

import json
from datetime import UTC, datetime, timedelta

from qdrant_client import QdrantClient

from insightforge_agent.domain.contracts import (
    Claim,
    ClaimVerdict,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    ExtractedFact,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportDraft,
    ReportSection,
    VerifiedReport,
)
from insightforge_agent.domain.diff import DiffEntry, EstablishedFinding
from insightforge_agent.domain.findings import Finding, FindingProvenance
from insightforge_agent.domain.models import (
    Observation,
    Passage,
    Report,
    Run,
    RunEvent,
    RunState,
    RunSummary,
    Source,
    SubTaskRecord,
)
from insightforge_agent.domain.plan import SubTask
from insightforge_agent.domain.watchlist import Baseline, BaselineSeed, WatchlistItem
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.scheduler.retention import RetentionSweeper
from insightforge_agent.stores.doc_chunks import COLLECTION, DocChunkIndex
from insightforge_agent.stores.source_store import PassageDraft, SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos

T0 = datetime(2030, 1, 1, tzinfo=UTC)
DAYS = 30
OLD = T0 - timedelta(days=DAYS + 10)  # finished well before the cutoff at T0
YOUNG = T0 - timedelta(days=1)


def ref(observation_id: str, index: int = 0) -> PassageRef:
    return PassageRef(observation_id=observation_id, index=index)


def item(eid: str, primary: PassageRef, supporting=(), contradicting=()) -> EvidenceItem:
    return EvidenceItem(id=eid, subtask_id="s1", aspect_id="a1", statement=f"{eid} statement.",
                        confidence="MEDIUM", primary=primary,
                        supporting=list(supporting) or [primary],
                        contradicting=list(contradicting), entity="Acme", predicate="price",
                        value="$100", scope="US", as_of_period="2029-12")


def finding(run_id: str, primary: PassageRef, supporting=(), value="$100") -> Finding:
    return Finding(
        key='["acme","price","us"]', subject="Acme", predicate="price", object=value,
        object_key=value, polarity="affirmed", scope="US", as_of_period="2029-12",
        provenance=FindingProvenance(
            run_id=run_id, report_id=f"rep_{run_id}", evidence_id=f"ev_{run_id}",
            claim="c", statement="s", primary=primary, supporting=list(supporting) or [primary],
            source_ids=[], observation_dates={primary.observation_id: OLD}))


class World:
    def __init__(self, path, days: int = DAYS):
        self.path, self.now, self.days = path, T0, days
        self.qdrant = QdrantClient(":memory:")
        self.index = DocChunkIndex(self.qdrant, HashingEmbedder(64), "test-hash", 64)
        self.open()

    def open(self):
        self.repos = SqliteRepos(str(self.path))
        self.store = SourceStore(self.repos.sources, self.repos.observations,
                                 self.repos.passages)
        self.sweeper = RetentionSweeper(self.repos.retention, self.index, lambda: self.now,
                                        retention_days=self.days)
        return self

    def restart(self):
        self.repos.close()
        return self.open()

    def sweep(self):
        return self.sweeper.sweep()

    # --- writing ----------------------------------------------------------------------------

    def run(self, run_id, state=RunState.PLANNING, owner="alice", created=OLD, item=None):
        self.repos.runs.add(Run(id=run_id, owner_id=owner, brief="EV market", state=state,
                                created_at=created, watchlist_item_id=item))

    def web(self, run_id, obs_id, paragraphs=3, owner="alice", source_id=None, url=None):
        """A web Observation of `run_id` with `paragraphs` Passages (texts p0, p1, ...)."""
        sid = source_id or f"src_{obs_id}"
        self.repos.sources.add(Source(id=sid, owner_id=owner, run_id=run_id, kind="web",
                                      locator=url or f"https://example.com/{sid}", title=sid))
        self.repos.passages.add_all(owner, obs_id, [
            Passage(observation_id=obs_id, index=i, text=f"{obs_id} p{i}")
            for i in range(paragraphs)])
        self.repos.observations.add(Observation(id=obs_id, owner_id=owner, source_id=sid,
                                                run_id=run_id, fetched_at=OLD))
        return obs_id

    def pdf(self, run_id, name="a.pdf", chunks=3, owner="alice") -> Observation:
        """An uploaded document of `run_id`, stored and indexed in doc_chunks."""
        obs = self.store.ingest_document(
            owner_id=owner, run_id=run_id, content_hash=f"h-{owner}-{name}", filename=name,
            chunks=[PassageDraft(f"{name} chunk {i}", page=i + 1) for i in range(chunks)],
            fetched_at=OLD)
        self.index.index(owner, run_id, self.repos.sources.get(owner, obs.source_id),
                         self.repos.passages.list(owner, obs.id))
        return obs

    def complete(self, run_id, at=OLD, evidence=(), historical=(), verdicts=(), owner="alice",
                 item=None, created=None):
        """A COMPLETE Run and its Report, finished `at`, whose body holds these parts."""
        self.run(run_id, RunState.COMPLETE, owner=owner, created=created or at, item=item)
        claims = [Claim(text=f"{e.statement}", evidence_id=e.id) for e in evidence]
        claims += [Claim(text=h.statement, evidence_id=h.id, historical=True)
                   for h in historical]
        body = {
            "brief": "EV market",
            "draft": ReportDraft(title="R", sections=[ReportSection(
                heading="Findings", claims=claims)]).model_dump(mode="json"),
            "verified": VerifiedReport(draft=ReportDraft(title="R", sections=[]),
                                       verdicts=list(verdicts)).model_dump(mode="json"),
            "evidence": EvidenceBundle(sections=[EvidenceSection(
                aspect_id="a1", heading="Findings", confidence="MEDIUM",
                items=list(evidence))]).model_dump(mode="json"),
            "historical": HistoricalContext(items=list(historical)).model_dump(mode="json"),
        }
        report = Report(id=f"rep_{run_id}", owner_id=owner, run_id=run_id, created_at=at,
                        body=body)
        self.repos.reports.add(report)
        self.repos.summaries.add(RunSummary(owner_id=owner, run_id=run_id, created_at=at,
                                            brief="EV market", memory_indexed=True))
        return report

    def failed(self, run_id, at=OLD, owner="alice", stage=RunState.RESEARCHING):
        self.run(run_id, stage, owner=owner)
        return self.repos.runs.fail(owner, run_id, reason="every Sub-task failed",
                                    error_codes=["pipeline_failure"], failed_at=at)

    def historical_item(self, run_id, primary, supporting=()) -> HistoricalItem:
        return HistoricalItem(id=f"hist_{run_id}", subtask_id="m1", aspect_id="a1",
                              heading="Findings", statement="Earlier claim.",
                              report_id=f"rep_{run_id}", run_id=run_id,
                              report_date=OLD.date(), primary=primary,
                              supporting=list(supporting))

    def verdict(self, original, matched) -> ClaimVerdict:
        return ClaimVerdict(section=0, claim=0, evidence_id="ev", verdict="verified",
                            check="web_refetch", original=original, matched=matched)

    def operational(self, run_id, owner="alice"):
        """Sub-task rows and progress events, as a real Run leaves them."""
        st = SubTask(id="s1", aspect_id="a1", query="q", source_type="web",
                     time_horizon_months=12, priority=1)
        self.repos.subtasks.save_plan(owner, run_id, [SubTaskRecord(run_id=run_id, subtask=st)])
        for type_ in ("stage", "source_summary", "doc_search", "run_failed"):
            self.repos.events.append(RunEvent(owner_id=owner, run_id=run_id, type=type_,
                                              payload={"reason": "r"}, created_at=OLD))

    def fact(self, run_id, passage: PassageRef, source_id: str, owner="alice",
             fid="fact_1") -> ExtractedFact:
        fact = ExtractedFact(id=fid, source_id=source_id, passage=passage,
                             statement="Acme price is $100.", entity="Acme", predicate="price",
                             value="$100", scope="US", period="2029-12")
        self.repos.entities.upsert_facts(owner, run_id, [fact])
        return fact

    def watchlist(self, item_id="w", owner="alice"):
        self.repos.watchlists.add(WatchlistItem(id=item_id, owner_id=owner, brief="EV market",
                                                cadence_minutes=60, created_at=OLD,
                                                next_tick_at=T0))

    def seed(self, run_id, evidence: EvidenceItem, observation_dates, item_id="w",
             owner="alice"):
        self.repos.watchlists.initialize_baseline(Baseline(
            owner_id=owner, watchlist_item_id=item_id, initialized_at=OLD,
            initializing_run_id=run_id, seeds=[BaselineSeed(
                claim=Claim(text="c", evidence_id=evidence.id), evidence=evidence,
                observation_dates=observation_dates, source_ids=[], report_id=f"rep_{run_id}",
                run_id=run_id)]))

    def establish(self, key, state: EstablishedFinding, item_id="w", owner="alice"):
        self.execute("INSERT INTO watchlist_findings VALUES (?,?,?,?)",
                     owner, item_id, key, state.model_dump_json())

    def diff_log(self, run_id, entries: list[DiffEntry], item_id="w", owner="alice"):
        detail = {"entries": [e.model_dump(mode="json") for e in entries], "skipped": []}
        self.execute("INSERT INTO watchlist_diffs (owner_id,item_id,run_id,report_id,status,"
                     "detail,updated_at) VALUES (?,?,?,?,'succeeded',?,?)",
                     owner, item_id, run_id, f"rep_{run_id}", json.dumps(detail),
                     T0.isoformat())

    def execute(self, sql, *args):
        with self.repos.lock, self.repos._db:
            self.repos._db.execute(sql, args)

    # --- reading ----------------------------------------------------------------------------

    def sql(self, sql, *args):
        with self.repos.lock:
            return self.repos._db.execute(sql, args).fetchall()

    def passages(self, obs_id, owner="alice") -> list[int]:
        return [p.index for p in self.repos.passages.list(owner, obs_id)]

    def has_observation(self, obs_id, owner="alice") -> bool:
        return bool(self.sql("SELECT 1 FROM observations WHERE owner_id=? AND id=?",
                             owner, obs_id))

    def has_source(self, source_id, owner="alice") -> bool:
        return bool(self.sql("SELECT 1 FROM sources WHERE owner_id=? AND id=?",
                             owner, source_id))

    def points(self, owner=None) -> list[dict]:
        scroll = self.qdrant.scroll(COLLECTION, limit=1000, with_payload=True)[0]
        return [p.payload for p in scroll if owner is None or p.payload["owner_id"] == owner]

    def point_keys(self, owner=None) -> set[tuple]:
        return {(p["owner_id"], p["run_id"], p["observation_id"], p["passage_index"])
                for p in self.points(owner)}

    def snapshot(self) -> dict:
        """Every row of every table the sweep may touch, for idempotency checks."""
        tables = ("runs", "reports", "run_summaries", "run_failures", "sources", "run_sources",
                  "observations", "split_observations", "passages", "entities", "subtasks",
                  "run_events", "watchlist_baselines", "watchlist_findings", "watchlist_diffs",
                  "watchlist_alerts")
        return {t: sorted(map(repr, self.sql(f"SELECT * FROM {t}"))) for t in tables}

