"""T20 B2: a one-shot Brief (`POST /research/run`) is at most 500 characters, counted in code
points on the raw text, and a longer one is 422 before any Run exists. The shared `Brief` and a
Watchlist Brief (up to 10,000) keep their own limits, including on a scheduled tick."""

import json
from pathlib import Path

import pytest

from insightforge_agent.api.research import ONE_SHOT_BRIEF_MAX_CHARS
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline import research as research_module
from tests.api.support import BRIEF, T0, App, wait_until
from tests.api.test_watchlist_scheduling import setup, tick
from tests.pipeline.test_clarification import Clock

FIXTURE = Path(__file__).parents[1] / "fixtures" / "briefs" / "one_shot_lengths.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def text_of(case) -> str:
    return case["prefix"] + case["unit"] * case["repeat"] + case["suffix"]


def padded(length: int) -> str:
    """A clear Brief (not the vague one the scripted Submission check refuses) of `length`."""
    text = f"{BRIEF} Consider pricing, deliveries and battery supply in each market."
    return (text + " " + "Detail." * length)[:length]


def test_the_shared_cases_have_the_lengths_they_claim():
    assert ONE_SHOT_BRIEF_MAX_CHARS == 500
    for case in CASES:
        assert len(text_of(case)) == case["length"], case["name"]


@pytest.mark.parametrize("case", [c for c in CASES if c["length"] <= 500],
                         ids=lambda c: c["name"])
def test_a_brief_of_at_most_500_code_points_is_accepted_as_sent(tmp_path, case):
    with App(tmp_path) as app:
        app.login("alice")
        text = text_of(case)
        response = app.client.post("/research/run", json={"brief": text})
        assert response.status_code == 202, response.text
        run = app.run(response.json()["run_id"])
        assert run.brief == text  # stored raw: the server neither trims nor cuts it
        app.wait(run.id)
        wait_until(lambda: app.runtime.executor.in_flight == 0)


@pytest.mark.parametrize("case", [c for c in CASES if c["length"] > 500],
                         ids=lambda c: c["name"])
def test_a_brief_over_500_code_points_is_422_and_dispatches_nothing(
        tmp_path, monkeypatch, case):
    submitted, carried = [], []
    real_submit = research_module.ResearchService.submit
    monkeypatch.setattr(research_module.ResearchService, "submit",
                        lambda self, *a, **kw: submitted.append(a) or real_submit(self, *a, **kw))
    monkeypatch.setattr(research_module, "carry_run",
                        lambda *a, **kw: carried.append(a))
    with App(tmp_path) as app:
        app.login("alice")
        response = app.client.post("/research/run", json={"brief": text_of(case)})
        assert response.status_code == 422, response.text
        [error] = response.json()["detail"]
        assert error["loc"] == ["body", "brief"] and error["type"] == "string_too_long"
        assert app.runtime.repos.runs.list_for_owner("alice") == []
        assert submitted == [] and carried == []
        assert app.runtime.executor.in_flight == 0


def test_the_limit_keeps_the_other_submission_checks(tmp_path):
    over = "a" * 501
    with App(tmp_path) as app:
        assert app.submit(over).status_code == 401  # no persona: still 401, not 422
        app.login("alice")
        assert app.submit("").status_code == 422  # min_length, unchanged
        response = app.submit(over, source_toggles={"satellite": True})
        assert response.status_code == 422
        assert {e["loc"][1] for e in response.json()["detail"]} == {"brief", "source_toggles"}
        assert app.runtime.repos.runs.list_for_owner("alice") == []


def test_the_shared_brief_contract_has_no_500_character_limit():
    assert Brief(text="a" * 10_000).text == "a" * 10_000


@pytest.mark.parametrize("length", [501, 10_000])
def test_a_watchlist_brief_over_500_characters_is_stored(tmp_path, length):
    with App(tmp_path) as app:
        app.login("alice")
        response = app.client.post("/watchlist", json={"brief": padded(length),
                                                       "cadence_minutes": 15})
        assert response.status_code == 201, response.text
        assert response.json()["item"]["brief"] == padded(length)


def test_a_watchlist_brief_keeps_its_10000_character_limit(tmp_path):
    with App(tmp_path) as app:
        app.login("alice")
        response = app.client.post("/watchlist", json={"brief": padded(10_001),
                                                       "cadence_minutes": 15})
        assert response.status_code == 422
        assert app.runtime.repos.watchlists.list_all() == []


def test_a_scheduled_tick_runs_a_watchlist_brief_over_500_characters(tmp_path):
    clock = Clock(T0)
    with App(tmp_path, now=clock) as app:
        item = setup(app, brief=padded(2_000))
        run = tick(app, clock, item)
        assert run is not None
        assert run.brief == padded(2_000) and run.watchlist_item_id == item.id
        assert app.wait(run.id).state == RunState.COMPLETE
        assert app.runtime.repos.watchlists.counts("alice", item.id)[0] == 1
