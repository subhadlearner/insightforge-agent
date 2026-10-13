"""A Watchlist Item's stored Runs and Reports on a SQLite file, with the T12 Diff service.

Runs are written straight to the repositories (no pipeline), so each test controls exactly
which Findings a Report holds. Models are scripted; nothing leaves the process."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from insightforge_agent.agents.finding_judge import FindingJudge
from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    PassageRef,
    ReportDraft,
    ReportSection,
    VerifiedReport,
)
from insightforge_agent.domain.findings import DEFAULT_REGISTRY
from insightforge_agent.domain.models import (
    Observation,
    Passage,
    Report,
    Run,
    RunState,
    Source,
)
from insightforge_agent.domain.watchlist import WatchlistItem
from insightforge_agent.scheduler.diff import DIFF_RETRY_DELAY, DiffService
from insightforge_agent.scheduler.watchlist import WatchlistScheduler
from insightforge_agent.scheduler.webhook import WebhookSender
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.scripted import ScriptedChat

NOW = datetime(2030, 1, 1, tzinfo=UTC)
OWNER, ITEM = "alice", "w"


class FakeScheduler:
    """Records date jobs instead of running them; tests fire them explicitly."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def add_job(self, func, trigger, run_date=None, args=(), id=None, **kw):
        self.jobs[id] = {"func": func, "run_date": run_date, "args": list(args)}

    def remove_job(self, job_id):
        from apscheduler.jobstores.base import JobLookupError
        if self.jobs.pop(job_id, None) is None:
            raise JobLookupError(job_id)

    def get_jobs(self):
        return [SimpleNamespace(id=i) for i in self.jobs]


def unsupported_reply(messages):
    """A well-behaved judge that maps nothing: every Claim it is asked about is unsupported
    (a polarity-only request therefore reads as affirmed)."""
    claims = json.loads(str(messages[-1].content))
    return AIMessage(content=json.dumps({"judgments": [
        {"index": c["index"], "predicate": "unsupported", "polarity": "affirmed"}
        for c in claims]}))


def empty_judge():
    return FindingJudge(ScriptedChat(reply=unsupported_reply), DEFAULT_REGISTRY)


def fact(value="$100", predicate="price", entity="Acme", period="2030-01", scope="US",
         derived=False, historical=False):
    return dict(value=value, predicate=predicate, entity=entity, period=period, scope=scope,
                derived=derived, historical=historical)


class World:
    def __init__(self, path, *, judge=None, webhook_url=None, post=None,
                 unconfirmed_after=3):
        self.path, self.now = path, NOW
        self.judge = judge or empty_judge()
        self.webhook_url, self.posted = webhook_url, []
        self.post = post or (lambda url, payload: self.posted.append((url, payload)))
        self.unconfirmed_after = unconfirmed_after
        self.open()

    def open(self):
        self.repos = SqliteRepos(str(self.path))
        self.scheduler = FakeScheduler()
        self.deps = SimpleNamespace(repos=self.repos, now=lambda: self.now, light_model=None)
        self.webhooks = WebhookSender(self.deps, self.scheduler, post=self.post)
        self.diffs = DiffService(self.deps, self.scheduler, judge=self.judge,
                                 webhooks=self.webhooks,
                                 webhook_url_for=lambda owner: self.webhook_url,
                                 unconfirmed_after=self.unconfirmed_after)
        self.schedules = WatchlistScheduler(self.deps, self.scheduler, None, diffs=self.diffs)
        return self

    def restart(self):
        """A new process: new connection, new scheduler, no in-memory state carried over."""
        self.repos.close()
        return self.open()

    def add_item(self):
        self.repos.watchlists.add(WatchlistItem(
            id=ITEM, owner_id=OWNER, brief="EV market", cadence_minutes=15, created_at=NOW,
            next_tick_at=NOW))

    def run(self, run_id, minute, state=RunState.COMPLETE, item=ITEM):
        self.repos.runs.add(Run(id=run_id, owner_id=OWNER, brief="EV market", state=state,
                                created_at=NOW + timedelta(minutes=minute),
                                watchlist_item_id=item))

    def complete(self, run_id, minute, *facts, item=ITEM, fetched=None):
        """A COMPLETE Run whose Report has one current Claim per fact, each on its own web
        Source fetched by this Run."""
        self.run(run_id, minute, item=item)
        fetched = fetched or NOW + timedelta(minutes=minute)
        items, claims = [], []
        for i, f in enumerate(facts):
            sid, oid = f"src_{run_id}_{i}", f"obs_{run_id}_{i}"
            statement = f"{f['entity']} {f['predicate']} is {f['value']}."
            self.repos.sources.add(Source(id=sid, owner_id=OWNER, run_id=run_id, kind="web",
                                          locator=f"https://example.com/{sid}", title=sid))
            self.repos.observations.add(Observation(id=oid, owner_id=OWNER, source_id=sid,
                                                    run_id=run_id, fetched_at=fetched))
            self.repos.passages.add_all(OWNER, oid, [Passage(observation_id=oid, index=0,
                                                             text=statement)])
            ref = PassageRef(observation_id=oid, index=0)
            eid = f"ev_{run_id}_{i}"
            items.append(EvidenceItem(
                id=eid, subtask_id="s1", aspect_id="a1", statement=statement,
                confidence="MEDIUM", primary=ref, supporting=[ref], entity=f["entity"],
                predicate=f["predicate"], value=f["value"], scope=f["scope"],
                as_of_period=f["period"],
                derived_from="observation_date" if f["derived"] else None))
            claims.append(Claim(text=statement, evidence_id=eid, historical=f["historical"]))
        verified = VerifiedReport(draft=ReportDraft(
            title="Report", sections=[ReportSection(heading="Findings", claims=claims)]))
        body = {"verified": verified.model_dump(mode="json"),
                "evidence": EvidenceBundle(sections=[EvidenceSection(
                    aspect_id="a1", heading="Findings", confidence="MEDIUM",
                    items=items)]).model_dump(mode="json")}
        self.repos.reports.add(Report(id=f"rep_{run_id}", owner_id=OWNER, run_id=run_id,
                                      created_at=NOW + timedelta(minutes=minute), body=body))

    def seed(self):
        return self.schedules.initialize(OWNER, ITEM)

    def process(self):
        self.diffs.process(OWNER, ITEM)

    def retry(self):
        """The retry job fires once its stored earliest time has come."""
        self.now += DIFF_RETRY_DELAY
        self.diffs.run_item(OWNER, ITEM)

    # --- reads ----------------------------------------------------------------------------

    def findings(self):
        return self.repos.diffs.findings(OWNER, ITEM)

    def values(self):
        return sorted((s.finding.subject, s.finding.predicate, s.finding.object,
                       s.finding.polarity) for s in self.findings()[1].values())

    def diff(self, run_id):
        return self.repos.diffs.diff(OWNER, ITEM, run_id)

    def alerts(self):
        return self.repos.diffs.alerts(OWNER)

    def sql(self, query, *args):
        with self.repos.lock:
            return self.repos._db.execute(query, args).fetchall()

    def execute(self, script):
        with self.repos.lock:
            self.repos._db.executescript(script)


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
