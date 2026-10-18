"""T8 (#9): the Memory researcher and Run summaries, end to end with scripted models, the
hashing embedder and in-memory Qdrant. No network, no paid calls.

Covers: Run summaries for the Planner (newest 3, same owner, same Watchlist Item or one-shot,
never a transcript); memory eligibility (own COMPLETE Reports only, never Failed Runs or another
User's, never the same Watchlist Item); per-Sub-task authorisation of memory references;
historical/current separation; memory-only Runs; and interruption-safe finalisation."""

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from qdrant_client import QdrantClient

from insightforge_agent.domain.contracts import (
    Brief,
    EvidenceBundle,
    HistoricalContext,
    ReportDraft,
)
from insightforge_agent.domain.errors import SubmissionRejected
from insightforge_agent.domain.memory import MemoryRef
from insightforge_agent.domain.models import Report, Run, RunState
from insightforge_agent.domain.watchlist import WatchlistItem
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.plan import AGENT_FOR_SOURCE_TYPE, SubTaskPlan, dispatched_id
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.pipeline.graph import (
    build_graph,
    outer_config,
    run_brief,
    settle_interrupted,
)
from insightforge_agent.pipeline.memory import (
    eligible_report_ids,
    finalise_report,
    report_finalised,
    resolve_historical,
)
from insightforge_agent.stores.report_sections import COLLECTION, ReportSectionIndex
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, ai, call

T0 = datetime(2026, 9, 1, 9, tzinfo=UTC)
MEMORY_QUERIES = {"m1": "BYD vehicles sold 2025 sales million",
                  "m2": "battery pack prices LFP cells percent",
                  "m3": "Tesla Model 3 price discounts Seal"}
WEB_AND_MEMORY = [("s1", "a1", "Sales", fakes.QUERIES["s1"], "web"),
                  ("s2", "a2", "Pricing", fakes.QUERIES["s2"], "web"),
                  ("m1", "a3", "Earlier findings", MEMORY_QUERIES["m1"], "memory")]
MEMORY_ONLY = [("m1", "a1", "Sales", MEMORY_QUERIES["m1"], "memory"),
               ("m2", "a2", "Batteries", MEMORY_QUERIES["m2"], "memory"),
               ("m3", "a3", "Pricing", MEMORY_QUERIES["m3"], "memory")]
HIST_LINE = re.compile(r"^- \[(hist_\w+)\] \((.+?); reported (\d{4}-\d{2}-\d{2}); as of [^)]*\) (.+)$",
                       re.MULTILINE)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


def planner(tasks) -> ScriptedChat:
    aspects = list(dict.fromkeys((a, name) for _, a, name, _, _ in tasks))
    plan = {"plan": {
        "aspects": [{"id": a, "name": name} for a, name in aspects],
        "sub_tasks": [{"id": sid, "aspect_id": a, "query": q, "source_type": t,
                       "time_horizon_months": 12, "priority": n}
                      for n, (sid, a, _, q, t) in enumerate(tasks, start=1)]}}
    todos = call("write_todos", {"todos": [{"content": q, "status": "pending"}
                                           for _, _, _, q, _ in tasks]}, "todo-1")
    dispatch = [call("task", {"description": f"SUBTASK_ID={sid} {q}",
                              "subagent_type": AGENT_FOR_SOURCE_TYPE[t]}, f"research-{sid}")
                for sid, _, _, q, t in tasks]
    return ScriptedChat(script=[ai(call("submit_plan", plan, "plan-1")), ai(text="Planned."),
                                ai(todos, *dispatch), ai(text="Dispatched.")])


def researchers(extra: dict[str, list[str]] | None = None, name_all: dict | None = None,
                seen: list | None = None) -> ScriptedChat:
    """Web Researchers behave as in `fakes`. A Memory Researcher searches with its query and
    names every ref it was given, plus `extra[sid]`; for a Sub-task in `name_all` it names the
    given refs instead (every Claim of a Report, see `claim_refs`: an overreach). `seen` records what Memory Researchers saw."""
    extra, name_all = extra or {}, name_all or {}
    web = fakes.researcher()

    def reply(messages) -> AIMessage:
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        if "Memory Researcher" not in system:
            return web.reply(messages)
        if seen is not None:
            seen.append([str(m.content) for m in messages])
        task = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        sid = dispatched_id(task)
        last = messages[-1]
        if isinstance(last, ToolMessage):
            refs = [h["ref"] for h in json.loads(str(last.content))]
            if sid in name_all:
                refs = list(name_all[sid])
            refs += extra.get(sid, [])
            if not refs:
                return AIMessage(content="FAILED: nothing in earlier reports")
            return AIMessage(content=f"RESULT: earlier findings | refs: {', '.join(refs)}")
        return ai(call("memory_search", {"query": task.split(" ", 1)[1]}, f"mem-{sid}"))
    return ScriptedChat(reply=reply)


def writer(mode: str = "good", bad_first: int = 0) -> ScriptedChat:
    """Cites every current item as fakes.writer does and every Historical item with its report
    date inline. For the first `bad_first` replies `mode` breaks a rule: "no_date" leaves the
    date out, "as_current" cites a Historical item as a current Claim, "mark_current" marks a
    current item historical, "omit_all" leaves out every Historical Claim, "omit_one" those of
    the first historical heading, "wrong_heading" puts Historical Claims under "Findings"."""
    state = {"n": 0}

    def reply(messages) -> AIMessage:
        user = str(messages[-1].content)
        state["n"] += 1
        bad = state["n"] <= bad_first
        sections: dict[str, list] = {}
        for n, (eid, text) in enumerate(re.findall(r"- \[(ev_\w+)\] \(\w+\) (.+)", user)):
            historical = bad and mode == "mark_current" and n == 0
            sections.setdefault("Findings", []).append(
                {"text": text, "evidence_id": eid, "historical": historical})
        found = HIST_LINE.findall(user)
        first = found[0][1] if found else None
        for hid, heading, day, statement in found:
            if bad and (mode == "omit_all" or (mode == "omit_one" and heading == first)):
                continue
            if bad and mode == "wrong_heading":
                heading = "Findings"
            text = f"{statement.rstrip('.')} (reported {day})."
            claim = {"text": text, "evidence_id": hid, "historical": True}
            if bad and mode == "no_date":
                claim["text"] = statement
            if bad and mode == "as_current":
                claim["historical"] = False
            sections.setdefault(heading, []).append(claim)
        return AIMessage(content=json.dumps({
            "title": "Report", "summary": "Summary.",
            "sections": [{"heading": h, "claims": c} for h, c in sections.items()]}))
    return ScriptedChat(reply=reply)


@pytest.fixture
def mem_deps(make_deps):
    def make(**overrides):
        parts = dict(report_index=ReportSectionIndex(QdrantClient(":memory:"), HashingEmbedder(),
                                                     "test-hash", 256),
                     now=Clock(), researcher_model=researchers(), writer_model=writer())
        parts.update(overrides)
        return make_deps(**parts)
    return make


def prior_run(deps, owner="alice", brief="EV market", item=None, day=0,
              toggles=None) -> Run:
    """A completed web Run on the fixture pages: 10 Claims, filed by the Writer stage under
    their Evidence sections ("Sales", "Pricing", "Batteries").
    A Watchlist Run uses its item's standing Brief and source choices."""
    deps.now.t = T0 + timedelta(days=day)
    deps.planner_model = fakes.planner()
    toggles = toggles or {"web": True}
    if item is not None:
        try:
            deps.repos.watchlists.get(owner, item)
        except NotFoundError:
            deps.repos.watchlists.add(WatchlistItem(id=item, owner_id=owner, brief=brief,
                cadence_minutes=15, created_at=deps.now(), next_tick_at=deps.now(),
                source_toggles=toggles))
    run = run_brief(deps, owner, Brief(text=brief, source_toggles=toggles),
                    watchlist_item_id=item)
    assert run.state == RunState.COMPLETE, events(deps, run, "run_failed")
    return run


MEMORY_BRIEF = "EV market, with earlier findings"
WEB_AND_MEMORY_TOGGLES = {"web": True, "memory": True}


def memory_run(deps, tasks=WEB_AND_MEMORY, owner="alice", item=None, day=30, web=True,
               snapshots=None) -> Run:
    deps.now.t = T0 + timedelta(days=day)
    deps.planner_model = planner(tasks)
    toggles = {"web": web, "memory": True}
    return run_brief(deps, owner, Brief(text=MEMORY_BRIEF, source_toggles=toggles),
                     watchlist_item_id=item, on_state=snapshots.append if snapshots is not None
                     else None)


def events(deps, run, type_=None):
    out, cursor = [], 0
    while batch := deps.repos.events.read_after(run.owner_id, run.id, cursor):
        out += batch
        cursor = batch[-1].seq
    return [e.payload for e in out if type_ is None or e.type == type_]


def report_of(deps, run) -> Report:
    return deps.repos.reports.get_for_run(run.owner_id, run.id)


def claims_of(report: Report):
    draft = ReportDraft.model_validate(report.body["draft"])
    return [c for s in draft.sections for c in s.claims]


def claim_refs(report: Report) -> list[str]:
    """Every Claim of the Report as a memory reference, `report:s<section>:c<claim>`."""
    draft = ReportDraft.model_validate(report.body["draft"])
    return [f"{report.id}:s{si}:c{ci}" for si, s in enumerate(draft.sections)
            for ci in range(len(s.claims))]


def plan_message(model: ScriptedChat) -> str:
    return next(str(m.content) for conv in model.seen for m in conv
                if isinstance(m, HumanMessage) and str(m.content).startswith("PLAN."))


# --- Run summaries for the Planner ----------------------------------------------------------

def test_a_completed_run_stores_one_bounded_summary(mem_deps):
    deps = mem_deps()
    run = prior_run(deps)
    summary = deps.repos.summaries.get_for_run("alice", run.id)
    assert summary.brief == "EV market" and summary.themes == ["Sales", "Pricing", "Batteries"]
    assert summary.key_entities == ["BYD"] and summary.memory_indexed
    assert summary.watchlist_item_id is None


def test_the_planner_reads_exactly_the_newest_three_summaries_and_no_transcript(mem_deps):
    deps = mem_deps()
    prior = [prior_run(deps, brief=f"EV market round {n}", day=n) for n in range(4)]
    old_claims = [c.text for r in prior for c in claims_of(report_of(deps, r))]
    deps.now.t = T0 + timedelta(days=10)
    deps.planner_model = fakes.planner()
    run = run_brief(deps, "alice", Brief(text="EV market today"))
    assert run.state == RunState.COMPLETE
    message = plan_message(deps.planner_model)
    assert [line for line in message.splitlines() if line.startswith("- Run of")] == [
        "- Run of 2026-09-04: Brief: EV market round 3",
        "- Run of 2026-09-03: Brief: EV market round 2",
        "- Run of 2026-09-02: Brief: EV market round 1"]
    seen = "\n".join(str(m.content) for conv in deps.planner_model.seen for m in conv)
    assert "EV market round 0" not in seen
    assert not any(text in seen for text in old_claims)  # no earlier Report body
    assert "RESULT:" not in plan_message(deps.planner_model)  # no earlier Planner messages
    (loaded,) = events(deps, run, "run_summaries_loaded")
    assert loaded == {"count": 3, "run_ids": [r.id for r in reversed(prior[1:])]}


def test_one_shot_summaries_exclude_other_users_and_watchlist_runs(mem_deps):
    deps = mem_deps()
    own = prior_run(deps, brief="own one-shot", day=0)
    prior_run(deps, owner="bob", brief="bob one-shot", day=1)
    prior_run(deps, brief="alice watchlist", item="w1", day=2)
    deps.planner_model = fakes.planner()
    run = run_brief(deps, "alice", Brief(text="EV market today"))
    assert events(deps, run, "run_summaries_loaded")[0]["run_ids"] == [own.id]
    message = plan_message(deps.planner_model)
    assert "own one-shot" in message
    assert "bob one-shot" not in message and "alice watchlist" not in message


def test_watchlist_summaries_are_for_the_same_item_only(mem_deps):
    deps = mem_deps()
    same = prior_run(deps, brief="w1 tick", item="w1", day=0)
    prior_run(deps, brief="w2 tick", item="w2", day=1)
    prior_run(deps, brief="one-shot", day=2)
    prior_run(deps, owner="bob", brief="bob w1 tick", item="w1", day=3)
    deps.planner_model = fakes.planner()
    run = run_brief(deps, "alice", Brief(text="w1 tick"), watchlist_item_id="w1")
    assert deps.repos.runs.get("alice", run.id).watchlist_item_id == "w1"
    assert events(deps, run, "run_summaries_loaded")[0]["run_ids"] == [same.id]
    assert "w1 tick" in plan_message(deps.planner_model)
    assert "bob w1 tick" not in plan_message(deps.planner_model)


def test_without_earlier_runs_the_plan_message_is_unchanged(mem_deps):
    deps = mem_deps()
    deps.planner_model = fakes.planner()
    run_brief(deps, "alice", Brief(text="EV market"))
    assert plan_message(deps.planner_model) == "PLAN. Allowed source types: web. Brief: EV market"


# --- memory eligibility and availability -------------------------------------------------------

def test_an_own_complete_report_makes_memory_available(mem_deps):
    deps = mem_deps()
    prior = prior_run(deps)
    assert eligible_report_ids(deps, "alice", None) == [report_of(deps, prior).id]
    run = memory_run(deps, MEMORY_ONLY, web=False)
    assert run.state == RunState.COMPLETE


def test_a_failed_run_never_creates_eligible_memory(mem_deps):
    deps = mem_deps(writer_model=fakes.writer(bad_first=99))
    deps.planner_model = fakes.planner()
    failed = run_brief(deps, "alice", Brief(text="EV market"))
    assert failed.state == RunState.FAILED
    assert eligible_report_ids(deps, "alice", None) == []
    assert not report_finalised(deps, "alice", failed.id)
    with pytest.raises(SubmissionRejected, match="no earlier Report is eligible"):
        run_brief(deps, "alice", Brief(text="x", source_toggles={"memory": True}))


def test_a_report_whose_run_is_not_complete_is_not_eligible(mem_deps):
    deps = mem_deps()
    prior = prior_run(deps)
    deps.repos.runs.set_state("alice", prior.id, RunState.FAILED)
    assert eligible_report_ids(deps, "alice", None) == []


def test_another_users_report_never_makes_memory_available(mem_deps):
    deps = mem_deps()
    prior_run(deps, owner="alice")
    assert eligible_report_ids(deps, "bob", None) == []
    with pytest.raises(SubmissionRejected, match="no earlier Report is eligible"):
        run_brief(deps, "bob", Brief(text="x", source_toggles={"memory": True}))
    snapshots = []
    deps.planner_model = fakes.planner()
    run_brief(deps, "bob", Brief(text="x", source_toggles={"web": True, "memory": True}),
              on_state=snapshots.append)
    assert snapshots[0]["allowed_types"] == ["web"] and snapshots[0]["memory_reports"] == []


def test_a_report_from_before_finalisation_existed_is_not_eligible(mem_deps):
    deps = mem_deps()
    deps.repos.runs.add(Run(id="run_legacy", owner_id="alice", brief="b",
                            state=RunState.COMPLETE, created_at=T0))
    deps.repos.reports.add(Report(id="rep_legacy", owner_id="alice", run_id="run_legacy",
                                  created_at=T0, body={}))
    assert eligible_report_ids(deps, "alice", None) == []


def test_reports_finalised_without_the_index_never_become_memory(make_deps, mem_deps):
    deps = make_deps(now=Clock())  # no report_sections index
    prior_run(deps)
    (summary,) = deps.repos.summaries.list_for("alice", None)
    assert not summary.memory_indexed
    assert eligible_report_ids(deps, "alice", None) == []


# --- the same Watchlist Item ----------------------------------------------------------------

def test_a_watchlist_run_never_searches_reports_of_its_own_item(mem_deps):
    deps = mem_deps()
    one_shot = report_of(deps, prior_run(deps, day=0)).id
    own_item = report_of(deps, prior_run(deps, brief=MEMORY_BRIEF, item="w1", day=1,
                                         toggles=WEB_AND_MEMORY_TOGGLES)).id
    other_item = report_of(deps, prior_run(deps, item="w2", day=2)).id
    assert eligible_report_ids(deps, "alice", "w1") == [one_shot, other_item]
    deps.researcher_model = researchers(extra={"m1": [f"{own_item}:s0:c0"]})
    snapshots = []
    run = memory_run(deps, item="w1", snapshots=snapshots)
    assert run.state == RunState.COMPLETE
    assert snapshots[0]["memory_reports"] == [one_shot, other_item]
    searched = {r.split(":")[0] for e in events(deps, run, "memory_search") for r in e["refs"]}
    assert searched and own_item not in searched
    historical = HistoricalContext.model_validate(report_of(deps, run).body["historical"])
    assert historical.items and own_item not in {h.report_id for h in historical.items}
    (rejected,) = events(deps, run, "memory_refs_rejected")
    assert rejected["refs"] == [f"{own_item}:s0:c0"]


def test_resolution_rejects_reports_outside_the_eligible_set_and_other_users(mem_deps):
    deps = mem_deps()
    alice_report = report_of(deps, prior_run(deps)).id
    plan = SubTaskPlan.model_validate({
        "aspects": [{"id": "a1", "name": "x"}],
        "sub_tasks": [{"id": "m1", "aspect_id": "a1", "query": "q", "source_type": "memory",
                       "time_horizon_months": 1, "priority": 1}]})
    ref = MemoryRef(alice_report, 0, 0)
    assert not resolve_historical(deps, "alice", "run_x", plan, {"m1": [ref]}, set()).context.items
    as_bob = resolve_historical(deps, "bob", "run_x", plan, {"m1": [ref]}, {alice_report})
    assert not as_bob.context.items and as_bob.rejected == {"m1": [ref]}
    missing = MemoryRef(alice_report, 0, 99)
    assert resolve_historical(deps, "alice", "run_x", plan, {"m1": [missing]},
                              {alice_report}).rejected == {"m1": [missing]}


# --- Sub-task authorisation of memory references -------------------------------------------------

def test_a_memory_subtask_may_use_only_refs_its_own_searches_returned(mem_deps):
    deps = mem_deps()
    prior_report = report_of(deps, prior_run(deps))
    prior, every_claim = prior_report.id, claim_refs(prior_report)
    tasks = [("m1", "a1", "Sales", MEMORY_QUERIES["m1"], "memory"),
             ("m2", "a2", "Batteries", MEMORY_QUERIES["m2"], "memory"),
             ("s1", "a3", "Market", fakes.QUERIES["s1"], "web")]
    invented = "rep_invented0000:s0:c0"
    deps.researcher_model = researchers(name_all={"m1": every_claim}, extra={"m2": [invented]})
    run = memory_run(deps, tasks)
    assert run.state == RunState.COMPLETE
    searched: dict[str, set[str]] = {}
    for e in events(deps, run, "memory_search"):
        searched.setdefault(e["subtask_id"], set()).update(e["refs"])
    assert set(searched) == {"m1", "m2"} and all(len(r) == 5 for r in searched.values())
    only_m2 = searched["m2"] - searched["m1"]
    assert only_m2  # m1 named these, but only m2's search returned them
    historical = HistoricalContext.model_validate(report_of(deps, run).body["historical"])
    by_task: dict[str, set[str]] = {}
    for h in historical.items:
        by_task.setdefault(h.subtask_id, set()).add(h.statement)
    rejected = {e["subtask_id"]: set(e["refs"]) for e in events(deps, run, "memory_refs_rejected")}
    assert len(every_claim) == 10
    assert rejected["m1"] == set(every_claim) - searched["m1"]
    assert rejected["m1"] >= only_m2
    assert rejected["m2"] == {invented}
    assert len(historical.items) == len(searched["m1"] | searched["m2"])


def test_the_memory_researcher_sees_references_and_metadata_never_report_text(mem_deps):
    seen: list = []
    deps = mem_deps()
    prior = report_of(deps, prior_run(deps))
    deps.researcher_model = researchers(seen=seen)
    memory_run(deps)
    assert seen
    texts = [c.text for c in claims_of(prior)]
    for conv in seen:
        joined = "\n".join(conv)
        assert not any(t in joined for t in texts) and "EV market" not in joined
        for content in conv:
            if content.startswith("[{"):
                hits = json.loads(content)
                assert 0 < len(hits) <= 5
                assert all(set(h) == {"ref", "report_date", "heading", "relevance_score"}
                           for h in hits)


# --- Historical Claims, never current Evidence ----------------------------------------------------

def test_memory_findings_become_historical_claims_with_original_lineage(mem_deps):
    deps = mem_deps()
    prior_run_ = prior_run(deps)
    prior = report_of(deps, prior_run_)
    prior_items = {i.id: i for s in EvidenceBundle.model_validate(prior.body["evidence"]).sections
                   for i in s.items}
    prior_claims = {c.text: prior_items[c.evidence_id] for c in claims_of(prior)}
    snapshots = []
    run = memory_run(deps, snapshots=snapshots)
    assert run.state == RunState.COMPLETE
    report = report_of(deps, run)
    historical = HistoricalContext.model_validate(report.body["historical"])
    bundle = EvidenceBundle.model_validate(report.body["evidence"])
    current = {i.id: i for s in bundle.sections for i in s.items}
    assert historical.items and current
    assert not set(historical.by_id()) & set(current)  # never inside the Evidence bundle
    for h in historical.items:  # lineage from the persisted Report, with its own date
        original = prior_claims[h.statement]
        assert (h.report_id, h.run_id, h.report_date) == (prior.id, prior_run_.id, date(2026, 9, 1))
        assert h.primary == original.primary and h.supporting == original.supporting
        assert h.as_of_period == original.as_of_period and h.subtask_id == "m1"
    for item in current.values():  # current Evidence rests only on this Run's Observations
        assert deps.repos.observations.get("alice", item.primary.observation_id).run_id == run.id
    research = next(s["research"] for s in reversed(snapshots) if "research" in s)
    (m1,) = [r for r in research["results"] if r["subtask_id"] == "m1"]
    assert m1["status"] == "succeeded" and m1["source_refs"] == []  # nothing to Extraction
    claims = claims_of(report)
    historical_claims = [c for c in claims if c.historical]
    assert {c.evidence_id for c in historical_claims} == set(historical.by_id())
    assert all("(reported 2026-09-01)" in c.text for c in historical_claims)
    assert all(c.evidence_id in current for c in claims if not c.historical)
    assert_historical_claims_placed(report)  # mixed web + memory


def test_historical_claims_cite_and_fact_check_their_original_sources(mem_deps):
    deps = mem_deps()
    prior_run(deps)
    run = memory_run(deps, MEMORY_ONLY, web=False)
    report = report_of(deps, run)
    historical = HistoricalContext.model_validate(report.body["historical"]).by_id()
    claims = claims_of(report)
    citations = report_citations(deps.store, "alice", report)
    for claim, citation in zip(claims, citations, strict=True):
        assert citation.passage == historical[claim.evidence_id].primary
    verdicts = report.body["verified"]["verdicts"]
    assert len(verdicts) >= 5
    assert {v["check"] for v in verdicts} == {"historical_web_refetch"}
    assert all(v["verdict"] == "verified" for v in verdicts)
    for v in verdicts:
        assert v["original"] == historical[v["evidence_id"]].primary.model_dump()


def test_a_memory_only_run_completes_without_current_evidence(mem_deps):
    deps = mem_deps()
    prior_run(deps)
    run = memory_run(deps, MEMORY_ONLY, web=False)
    assert run.state == RunState.COMPLETE
    report = report_of(deps, run)
    assert report.body["evidence"] == {"sections": []}
    claims = claims_of(report)
    assert claims and all(c.historical for c in claims)
    assert_historical_claims_placed(report)  # memory only
    # Its own Claims are indexed in turn, with their lineage flattened to the first Report.
    later = HistoricalContext.model_validate(report.body["historical"])
    assert {h.report_id for h in later.items} == {report_of(deps, deps.repos.runs.list_for_owner(
        "alice")[0]).id}
    assert report.id in eligible_report_ids(deps, "alice", None)


def test_a_reused_historical_claim_keeps_its_original_report_and_passage(mem_deps):
    deps = mem_deps()
    first = report_of(deps, prior_run(deps))
    second = report_of(deps, memory_run(deps, MEMORY_ONLY, web=False, day=30))
    # A third Run may only search the second Report: its Claims were themselves historical.
    deps.repos.runs.set_state("alice", first.run_id, RunState.FAILED)
    third = report_of(deps, memory_run(deps, MEMORY_ONLY, web=False, day=60))
    items = HistoricalContext.model_validate(third.body["historical"]).items
    first_items = {i.primary for s in EvidenceBundle.model_validate(first.body["evidence"]).sections
                   for i in s.items}
    assert items and {h.report_id for h in items} == {first.id}
    assert all(h.report_date == date(2026, 9, 1) and h.primary in first_items for h in items)
    assert all("(reported 2026-09-01)" in c.text and "2026-10-01" not in c.text
               for c in claims_of(third))
    assert second.id != first.id


@pytest.mark.parametrize("mode, tasks, problem", [
    ("no_date", WEB_AND_MEMORY, "does not show its report date inline"),
    ("as_current", WEB_AND_MEMORY, "as current evidence"),
    ("mark_current", WEB_AND_MEMORY, "current evidence, never historical"),
    # Review round: background ignored while current Claims are valid, one historical Aspect
    # left out while another is used, and a Historical Claim under the wrong heading.
    ("omit_all", WEB_AND_MEMORY, "headed 'Earlier findings' has no historical claim"),
    ("omit_one", MEMORY_ONLY, "has no historical claim"),
    ("wrong_heading", WEB_AND_MEMORY, "is under 'Findings'; it belongs in the section headed"),
])
def test_writer_retries_when_a_claim_blurs_historical_and_current(mem_deps, mode, tasks,
                                                                   problem):
    deps = mem_deps()
    prior_run(deps)
    deps.writer_model = writer(mode, bad_first=1)
    run = memory_run(deps, tasks, web=tasks is WEB_AND_MEMORY)
    assert run.state == RunState.COMPLETE
    (violation,) = events(deps, run, "writer_violations")
    assert any(problem in p for p in violation["problems"])
    if mode == "omit_one":  # another heading was used, so only the omitted one is missing
        headings = {h.heading for h in HistoricalContext.model_validate(
            report_of(deps, run).body["historical"]).items}
        missing = [p for p in violation["problems"] if "has no historical claim" in p]
        assert len(headings) >= 2 and len(missing) == 1
    assert_historical_claims_placed(report_of(deps, run))  # the retry fixed it


def assert_historical_claims_placed(report: Report) -> None:
    """Every Historical Claim sits under its item's heading, and every heading has one."""
    items = HistoricalContext.model_validate(report.body["historical"]).by_id()
    draft = ReportDraft.model_validate(report.body["draft"])
    placed = {s.heading for s in draft.sections for c in s.claims
              if c.historical and items[c.evidence_id].heading == s.heading}
    assert all(items[c.evidence_id].heading == s.heading
               for s in draft.sections for c in s.claims if c.historical)
    assert placed == {h.heading for h in items.values()}


def test_a_writer_that_never_shows_the_date_fails_the_run(mem_deps):
    deps = mem_deps()
    prior_run(deps)
    deps.writer_model = writer("no_date", bad_first=99)
    run = memory_run(deps, MEMORY_ONLY, web=False)
    assert run.state == RunState.FAILED
    assert not deps.repos.reports.list_for_owner("alice")[1:]


# --- finalisation: idempotent and interruption-safe -----------------------------------------------

def test_finalisation_is_idempotent(mem_deps):
    deps = mem_deps()
    run = prior_run(deps)
    report = report_of(deps, run)
    points = deps.report_index._client.count(COLLECTION).count
    first = deps.repos.summaries.get_for_run("alice", run.id)
    deps.now.t = T0 + timedelta(days=5)
    assert finalise_report(deps, deps.repos.runs.get("alice", run.id), report) == first
    assert deps.report_index._client.count(COLLECTION).count == points == 10
    assert len(deps.repos.summaries.list_for("alice", None)) == 1


class Crash(BaseException):
    """Stands in for the process dying: nothing in the pipeline catches it."""


def start(deps, run_id="run_c", item=None):
    run = Run(id=run_id, owner_id="alice", brief="EV market", state=RunState.PLANNING,
              created_at=deps.now(), watchlist_item_id=item)
    deps.repos.runs.add(run)
    thread = f"thread-{uuid.uuid4()}"
    initial = {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": "EV market",
               "allowed_types": ["web"], "watchlist_item_id": item, "memory_reports": []}
    deps.planner_model = fakes.planner()
    return run, initial, outer_config(thread)


def test_a_crash_after_finalising_but_before_storing_the_report_is_safe(mem_deps):
    deps = mem_deps()
    run, initial, config = start(deps)
    reports, armed = deps.repos.reports, {"on": True}
    real_add = reports.add

    def add(report):
        if armed["on"]:
            armed["on"] = False
            raise Crash
        return real_add(report)

    reports.add = add
    with pytest.raises(Crash):
        build_graph(deps).invoke(initial, config)
    # Summarised and indexed under a Report id that was never stored: not COMPLETE, so neither
    # memory nor a Planner summary.
    assert deps.repos.runs.get("alice", run.id).state == RunState.FACT_CHECKING
    assert eligible_report_ids(deps, "alice", None) == []
    build_graph(deps).invoke(None, config)  # resume from the checkpoint
    (stored,) = deps.repos.reports.list_for_owner("alice")
    assert deps.repos.runs.get("alice", run.id).state == RunState.COMPLETE
    assert eligible_report_ids(deps, "alice", None) == [stored.id]
    hits = deps.report_index.search("alice", [stored.id], "BYD sales", k=3)
    assert hits and {h.ref.report_id for h in hits} == {stored.id}
    # The retry finalised the same Report id, so no orphan points were left behind.
    assert deps.report_index._client.count(COLLECTION).count == len(claims_of(stored)) == 10


def test_a_crash_after_storing_the_report_recovers_as_complete_and_eligible(mem_deps):
    deps = mem_deps()
    runs, real = deps.repos.runs, deps.repos.runs.set_state

    def flaky(owner, run_id, state):
        if state == RunState.COMPLETE and not getattr(flaky, "done", False):
            flaky.done = True
            raise RuntimeError("database hiccup")
        return real(owner, run_id, state)

    runs.set_state = flaky
    deps.planner_model = fakes.planner()
    run = run_brief(deps, "alice", Brief(text="EV market"))
    assert run.state == RunState.COMPLETE
    assert events(deps, run, "completion_recovered")
    assert eligible_report_ids(deps, "alice", None) == [report_of(deps, run).id]


def test_an_unfinalised_stored_report_is_finalised_before_complete(mem_deps):
    deps = mem_deps()
    run, initial, config = start(deps)
    build_graph(deps, interrupt_after=["fact_checking"]).invoke(initial, config)
    values = deps.checkpointer.get_tuple(config).checkpoint["channel_values"]
    legacy = Report(id="rep_legacy", owner_id="alice", run_id=run.id, created_at=deps.now(),
                    body={"brief": "EV market", "draft": values["draft"],
                          "verified": values["verified"], "evidence": values["bundle"]})
    deps.repos.reports.add(legacy)  # as if stored by an interrupted, unfinalised completion
    build_graph(deps).invoke(None, config)
    assert deps.repos.runs.get("alice", run.id).state == RunState.COMPLETE
    assert report_finalised(deps, "alice", run.id)
    assert eligible_report_ids(deps, "alice", None) == ["rep_legacy"]
    assert deps.report_index.search("alice", ["rep_legacy"], "BYD sales")


class BrokenIndex:
    def index(self, *args, **kwargs):
        raise ConnectionError("Qdrant is down")


def test_a_report_that_cannot_be_finalised_never_recovers_as_complete(mem_deps):
    deps = mem_deps()
    run, initial, config = start(deps)
    build_graph(deps, interrupt_after=["fact_checking"]).invoke(initial, config)
    values = deps.checkpointer.get_tuple(config).checkpoint["channel_values"]
    deps.repos.reports.add(Report(
        id="rep_legacy", owner_id="alice", run_id=run.id, created_at=deps.now(),
        body={"brief": "EV market", "draft": values["draft"], "verified": values["verified"],
              "evidence": values["bundle"]}))
    deps.report_index = BrokenIndex()
    with pytest.raises(ConnectionError):
        build_graph(deps).invoke(None, config)
    settled = settle_interrupted(deps, "alice", run.id, "ConnectionError: Qdrant is down")
    assert settled.state == RunState.FAILED
    assert eligible_report_ids(deps, "alice", None) == []
    (failed,) = events(deps, settled, "run_failed")
    assert "could not be finalised" in failed["reason"]


def test_an_index_outage_at_completion_fails_the_run_without_a_report(mem_deps):
    deps = mem_deps(report_index=BrokenIndex())
    deps.planner_model = fakes.planner()
    run = run_brief(deps, "alice", Brief(text="EV market"))
    assert run.state == RunState.FAILED
    assert deps.repos.reports.list_for_owner("alice") == []


def test_a_report_with_no_claim_to_index_is_not_memory(mem_deps):
    deps = mem_deps()
    prior = prior_run(deps)
    report = report_of(deps, prior)
    # Every Claim marked unverified: none may be reused, so nothing is indexed.
    verified = dict(report.body["verified"])
    verified["verdicts"] = [
        {"section": si, "claim": ci, "verdict": "unverified"}
        for si, s in enumerate(report.body["verified"]["draft"]["sections"])
        for ci in range(len(s["claims"]))]
    deps.repos.runs.add(Run(id="run_bad", owner_id="alice", brief="b", state=RunState.COMPLETE,
                            created_at=T0))
    bad = Report(id="rep_bad", owner_id="alice", run_id="run_bad", created_at=T0,
                 body={**report.body, "verified": verified})
    summary = finalise_report(deps, deps.repos.runs.get("alice", "run_bad"), bad)
    deps.repos.reports.add(bad)
    assert not summary.memory_indexed
    assert eligible_report_ids(deps, "alice", None) == [report.id]
