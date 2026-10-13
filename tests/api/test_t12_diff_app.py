"""T12 CP5: the whole app (real APScheduler, executor and SQLite files) with scripted models and
fixture search. A Watchlist Item's first Run establishes the Baseline; a later Run whose page
changed raises one Alert, delivered to a Slack-format webhook; absence raises none; a restart
neither repeats nor loses work."""

import json
import re

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.models import RunState
from tests.api.support import App, Gate, T0, wait_until
from tests.api.test_t11_lifecycle import setup, tick
from tests.pipeline import fakes
from tests.pipeline.test_t9_clarification import Clock
from tests.scripted import ScriptedChat

PRICE_URL = "https://www.example-auto-news.com/pricing-war"
PRICE = "Tesla cut the starting price of the Model 3 to {} dollars in China."
URL = "https://hooks.example.test/services/T/B/x"


class MutableSearch(fakes.FixtureSearch):
    """The pricing page says whatever `price` is now; None makes its fetch fail."""

    def __init__(self):
        super().__init__()
        self.price = "32,000"

    def fetch(self, url):
        if url == PRICE_URL:
            if self.price is None:
                raise fakes.FetchError(url)
            self._data["pages"][url]["body"] = PRICE.format(self.price)
        return super().fetch(url)


def structured_light():
    """fakes.light(), except: the price sentence is drafted as a structured fact, entailment
    says the passage asserts a present state (so the period is derived from the fetch), and
    the Finding judge leaves every unstructured Claim unsupported."""
    base = fakes.light()

    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        if system.startswith("You normalise market-intelligence claims"):
            claims = json.loads(user)
            return AIMessage(content=json.dumps({"judgments": [
                {"index": c["index"], "predicate": "unsupported", "polarity": "affirmed"}
                for c in claims]}))
        if system.startswith("You draft evidence"):
            items = []
            for i, text in re.findall(r"\[(\d+)\] (.+)", user):
                item = {"statement": text.strip(), "passages": [int(i)], "entities": []}
                m = re.search(r"Model 3 to ([\d,]+ dollars) in China", text)
                if m:
                    item.update(entity="Model 3", predicate="starting_price",
                                value=m.group(1), scope="China")
                items.append(item)
            return AIMessage(content=json.dumps({"items": items}))
        if system.startswith("You judge whether"):
            verdicts = [{"passage": int(i), "verdict": "SUPPORTED",
                         "asserts_present_state": True}
                        for i in re.findall(r"^\[(\d+)\] ", user, re.MULTILINE)]
            return AIMessage(content=json.dumps({"verdicts": verdicts}))
        return base.reply(messages)
    return ScriptedChat(reply=reply)


def make_app(tmp_path, clock, search):
    app = App(tmp_path, gate=Gate(search), now=clock,
              deps_overrides={"light_model": structured_light()})
    diffs = app.runtime.watchlists.schedules.diffs
    diffs.webhook_url_for = lambda owner_id: URL if owner_id == "alice" else None
    posted = []
    diffs.webhooks.post = lambda url, payload: posted.append((url, payload))
    return app, posted


def complete_tick(app, clock, item):
    run = tick(app, clock, item)
    assert run is not None
    assert app.wait(run.id).state == RunState.COMPLETE
    wait_until(lambda: app.runtime.executor.in_flight == 0)
    return run


def diff_done(app, item, run_id):
    record = app.runtime.repos.diffs.diff("alice", item.id, run_id)
    return record is not None and record.status != "pending"


def test_watchlist_runs_establish_diff_alert_and_deliver(tmp_path):
    clock, search = Clock(T0), MutableSearch()
    app, posted = make_app(tmp_path, clock, search)
    with app:
        item = setup(app, cadence_minutes=15, max_runs=4)
        diffs = app.runtime.repos.diffs
        first = complete_tick(app, clock, item)
        wait_until(lambda: app.runtime.repos.watchlists.baseline_state("alice", item.id) == "established")
        [price] = [s.finding for s in diffs.findings("alice", item.id)[1].values()]
        assert (price.subject, price.predicate, price.object, price.scope) == \
            ("Model 3", "price", "32,000 dollars", "China")
        assert price.derived and price.provenance.run_id == first.id
        assert diffs.alerts("alice") == []  # the first Run raises no Alert

        search.price = "29,000"
        second = complete_tick(app, clock, item)
        wait_until(lambda: diff_done(app, item, second.id))
        assert diffs.diff("alice", item.id, second.id).status == "succeeded"
        [alert] = diffs.alerts("alice")
        [change] = alert.changes
        assert (change.outcome, change.previous_object, change.object) == \
            ("changed", "32,000 dollars", "29,000 dollars")
        assert change.when.startswith("Change detected on ")
        report_of = {r["run_id"]: r["id"] for r in app.client.get("/reports").json()}
        assert (alert.report_id, change.previous_report_id) == \
            (report_of[second.id], report_of[first.id])
        wait_until(lambda: diffs.delivery("alice", alert.id).status == "delivered")
        [(url, payload)] = posted
        assert url == URL and "29,000 dollars" in payload["text"]

        search.price = None  # the price is absent from the next Run
        third = complete_tick(app, clock, item)
        wait_until(lambda: diff_done(app, item, third.id))
        assert len(diffs.alerts("alice")) == 1
        [state] = diffs.findings("alice", item.id)[1].values()
        assert (state.finding.object, state.not_reconfirmed) == ("29,000 dollars", 1)
        for run in (first, second, third):
            assert app.run(run.id).state == RunState.COMPLETE
        # The dashboard reports the explicit Baseline state; the seed is untouched.
        data = app.client.get(f"/watchlist/{item.id}").json()
        assert data["baseline_state"] == "established"
        assert data["baseline"]["initializing_run_id"] == first.id
        alerts_before = diffs.alerts("alice")

    # A restart repeats nothing: no new Diff, Alert or POST.
    app2, posted2 = make_app(tmp_path, clock, search)
    with app2:
        diffs2 = app2.runtime.repos.diffs
        wait_until(lambda: app2.runtime.scheduler.get_job(
            f"watchlist-diff:alice:{item.id}") is None)
        assert diffs2.alerts("alice") == alerts_before
        assert posted2 == []
        assert [d.status for d in diffs2.diffs("alice", item.id)] == ["succeeded"] * 2


def test_one_shot_runs_are_not_diffed(tmp_path):
    clock, search = Clock(T0), MutableSearch()
    app, posted = make_app(tmp_path, clock, search)
    with app:
        app.login("alice")
        run_id = app.submit().json()["run_id"]
        assert app.wait(run_id).state == RunState.COMPLETE
        wait_until(lambda: app.runtime.executor.in_flight == 0)
        with app.runtime.repos.lock:
            rows = app.runtime.repos._db.execute(
                "SELECT count(*) FROM watchlist_diffs").fetchone()
        assert rows == (0,) and app.runtime.repos.diffs.alerts("alice") == []
        assert not any(j.id.startswith("watchlist-diff") for j in
                       app.runtime.scheduler.get_jobs())
