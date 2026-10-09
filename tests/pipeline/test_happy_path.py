"""T3: a Brief goes in, a Report comes out, web search only, scripted models, recorded search."""

import json

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState, SubTaskStatus
from insightforge_agent.pipeline.graph import run_brief
from tests.pipeline import fakes
from tests.scripted import ScriptedChat

BRIEF = "Compare BYD and Tesla in electric vehicles: sales, pricing, battery technology."


def events(deps, run):
    return deps.repos.events.read_after("alice", run.id)


def test_brief_reaches_complete_and_stores_a_report(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE, [e.payload for e in events(deps, run)]
    stages = [e.payload["state"] for e in events(deps, run) if e.type == "stage"]
    assert stages[0] == "PLANNING" and stages[-1] == "COMPLETE"  # INGESTING skipped
    report = deps.repos.reports.get_for_run("alice", run.id)
    claims = [c for s in report.body["draft"]["sections"] for c in s["claims"]]
    assert claims
    evidence = {i["id"]: i for s in report.body["evidence"]["sections"] for i in s["items"]}
    assert all(c["evidence_id"] in evidence for c in claims)
    assert {r.status for r in deps.repos.subtasks.list("alice", run.id)} == {SubTaskStatus.SUCCEEDED}


def test_report_is_private_to_its_owner(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert deps.repos.reports.list_for_owner("bob") == []
    assert len(deps.repos.reports.list_for_owner("alice")) == 1
    assert run.owner_id == "alice"


def test_graph_state_carries_no_source_body(make_deps):
    big = "A very long paragraph about electric vehicle sales volumes. " * 400  # ~23k chars
    search = fakes.FixtureSearch()
    search._data["pages"]["https://www.example-auto-news.com/byd-sales-2025"]["body"] += "\n\n" + big
    deps = make_deps(search=search, fetcher=search)
    snapshots = []
    run = run_brief(deps, "alice", Brief(text=BRIEF), on_state=snapshots.append)
    assert run.state == RunState.COMPLETE
    stored = max(len(p.text) for s in deps.repos.sources.list_for_run("alice", run.id)
                 for o in deps.repos.observations.list_for_source("alice", s.id)
                 for p in deps.repos.passages.list("alice", o.id))
    assert stored > 20_000  # the body really is big, and really is in the store
    assert snapshots
    for snap in snapshots:
        assert len(json.dumps(snap)) < 20_000
        assert "A very long paragraph" not in json.dumps(snap)


def test_fetch_tool_summarises_and_the_researcher_sees_only_refs(make_deps):
    researcher, light = fakes.researcher(), fakes.light()
    deps = make_deps(researcher_model=researcher, light_model=light)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    bodies = ["4.27 million vehicles", "17 million in 2025", "32,000 dollars"]
    researcher_text = "\n".join(str(m.content) for conv in researcher.seen for m in conv)
    assert not any(b in researcher_text for b in bodies)
    tool_replies = [json.loads(str(m.content)) for conv in researcher.seen for m in conv
                    if m.type == "tool"]
    assert tool_replies
    for refs in tool_replies:
        for ref in refs:
            assert set(ref) == {"source_id", "title", "summary"}
            assert ref["summary"] == "A page about electric vehicles."
    summary_calls = [conv for conv in light.seen if "single plain sentence" in str(conv[0].content)]
    assert summary_calls  # the summary was written in the tool's own call, which did see the body
    assert any("4.27 million" in str(conv[-1].content) for conv in summary_calls)


def test_paywalled_page_is_skipped_and_logged(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    skipped = [e.payload for e in events(deps, run) if e.type == "source_skipped"]
    assert [s["reason"] for s in skipped] == ["paywalled"]


def test_dropped_subtask_is_repaired_once(make_deps):
    deps = make_deps(planner_model=fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",)))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert any(e.type == "repair" and e.payload["missing"] == ["s3"] for e in events(deps, run))
    assert {r.status for r in deps.repos.subtasks.list("alice", run.id)} == {SubTaskStatus.SUCCEEDED}


def test_missing_subtask_after_repair_is_listed_as_a_gap(make_deps):
    deps = make_deps(planner_model=fakes.planner(research_ids=("s1", "s2"), repair_ids=()))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    report = deps.repos.reports.get_for_run("alice", run.id)
    assert any("s3" in g for g in report.body["draft"]["gaps"])


def test_writer_is_retried_then_succeeds(make_deps):
    deps = make_deps(writer_model=fakes.writer(bad_first=2))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert len([e for e in events(deps, run) if e.type == "writer_violations"]) == 2


def test_writer_exhausting_retries_fails_the_run_with_no_report(make_deps):
    deps = make_deps(writer_model=fakes.writer(bad_first=99))
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert deps.repos.reports.list_for_owner("alice") == []
    (failed,) = [e for e in events(deps, run) if e.type == "run_failed"]
    assert "Writer" in failed.payload["reason"]


def test_every_subtask_failing_fails_the_run(make_deps):
    search = fakes.FixtureSearch()
    for url, page in search._data["pages"].items():
        page.clear()
        page["error"] = True
    deps = make_deps(search=search, fetcher=search)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.FAILED
    assert deps.repos.reports.list_for_owner("alice") == []


def test_run_passes_through_fact_checking_before_complete(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    stages = [e.payload["state"] for e in events(deps, run) if e.type == "stage"]
    assert stages == ["PLANNING", "RESEARCHING", "EXTRACTING", "SYNTHESIZING", "WRITING",
                      "FACT_CHECKING", "COMPLETE"]
    assert run.state == RunState.COMPLETE
    assert len(deps.repos.reports.list_for_owner("alice")) == 1


def test_run_state_is_fact_checking_while_that_stage_runs(make_deps):
    deps = make_deps()
    seen = []
    run_brief(deps, "alice", Brief(text=BRIEF),
              on_state=lambda s: seen.append(deps.repos.runs.get("alice", s["run_id"]).state))
    assert RunState.FACT_CHECKING in seen
    assert seen.index(RunState.FACT_CHECKING) > seen.index(RunState.WRITING)


def test_stored_report_is_the_verified_report(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    body = deps.repos.reports.get_for_run("alice", run.id).body
    assert set(body["verified"]) >= {"draft", "verdicts", "summary"}
    assert body["verified"]["draft"] == body["draft"]
    assert body["verified"]["summary"]["implemented"] is False


SENTINEL = "SENTINEL-BODY-7731"


def _all_checkpointed_bytes(saver) -> bytes:
    rows = saver.conn.execute("SELECT checkpoint FROM checkpoints").fetchall()
    rows += saver.conn.execute("SELECT value FROM writes").fetchall()
    return b"".join(bytes(r[0]) for r in rows if r[0] is not None)


def test_no_source_body_in_any_checkpoint_or_intermediate_state(make_deps):
    big = f"{SENTINEL} " + "A very long paragraph about electric vehicle sales volumes. " * 400
    search = fakes.FixtureSearch()
    search._data["pages"]["https://www.example-auto-news.com/byd-sales-2025"]["body"] += "\n\n" + big
    deps = make_deps(search=search, fetcher=search)
    snapshots = []
    run = run_brief(deps, "alice", Brief(text=BRIEF), on_state=snapshots.append)
    assert run.state == RunState.COMPLETE
    stored = [p.text for s in deps.repos.sources.list_for_run("alice", run.id)
              for o in deps.repos.observations.list_for_source("alice", s.id)
              for p in deps.repos.passages.list("alice", o.id)]
    assert any(SENTINEL in t for t in stored)  # the body is in the store, so the test can fail
    assert not any(SENTINEL in json.dumps(s) for s in snapshots)
    checkpointed = _all_checkpointed_bytes(deps.checkpointer)
    assert len(checkpointed) > 1000  # both the Planner thread and the outer graph checkpoint
    assert SENTINEL.encode() not in checkpointed


def test_pipeline_continues_from_the_checkpoint_without_in_memory_summaries(make_deps):
    import uuid

    from insightforge_agent.domain.models import Run
    from insightforge_agent.pipeline.graph import build_graph, outer_config

    deps = make_deps()
    run = Run(id="run_x", owner_id="alice", brief=BRIEF, state=RunState.PLANNING,
              created_at=deps.now())
    deps.repos.runs.add(run)
    thread = f"thread-{uuid.uuid4()}"
    config = outer_config(thread)
    initial = {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": BRIEF}
    build_graph(deps, interrupt_after=["collect"]).invoke(initial, config)
    assert deps.repos.runs.get("alice", run.id).state == RunState.RESEARCHING

    # A fresh Deps, as after a restart: same stores and checkpointer, nothing else carried
    # over. Its models would fail the test if the Planner or a Researcher were called again.
    class Forbidden(ScriptedChat):
        pass

    restarted = make_deps(
        repos=deps.repos, store=deps.store, checkpointer=deps.checkpointer,
        planner_model=Forbidden(script=[]), researcher_model=Forbidden(script=[]),
    )
    from insightforge_agent.pipeline.graph import summaries_from_log

    assert len(summaries_from_log(restarted, "alice", run.id)) >= 3  # durable, not in memory
    build_graph(restarted).invoke(None, config)
    assert restarted.repos.runs.get("alice", run.id).state == RunState.COMPLETE
    assert len(restarted.repos.reports.list_for_owner("alice")) == 1
    research = deps.checkpointer.get_tuple(config).checkpoint["channel_values"]["research"]
    assert all(set(ref) == {"source_id", "title", "summary"}
               for r in research["results"] for ref in r["source_refs"])


class Crash(BaseException):
    """Stands in for the process dying: nothing in the pipeline catches it."""


def _crash_once_on_complete(deps):
    runs, armed = deps.repos.runs, {"on": True}
    real = runs.set_state

    def set_state(owner, run_id, state):
        if state == RunState.COMPLETE and armed["on"]:
            armed["on"] = False
            raise Crash
        return real(owner, run_id, state)

    runs.set_state = set_state


def test_completion_retried_after_a_crash_reuses_the_stored_report(make_deps):
    import uuid

    from insightforge_agent.domain.models import Run
    from insightforge_agent.pipeline.graph import build_graph, outer_config

    deps = make_deps()
    run = Run(id="run_y", owner_id="alice", brief=BRIEF, state=RunState.PLANNING,
              created_at=deps.now())
    deps.repos.runs.add(run)
    thread = f"thread-{uuid.uuid4()}"
    config = outer_config(thread)
    initial = {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": BRIEF}
    _crash_once_on_complete(deps)
    try:
        build_graph(deps).invoke(initial, config)
        raise AssertionError("expected the simulated crash")
    except Crash:
        pass
    # Crashed after the insert and before COMPLETE: the Report exists, the Run is not FAILED.
    (stored,) = deps.repos.reports.list_for_owner("alice")
    assert deps.repos.runs.get("alice", run.id).state == RunState.FACT_CHECKING

    build_graph(deps).invoke(None, config)  # resume from the checkpoint
    reports = deps.repos.reports.list_for_owner("alice")
    assert [r.id for r in reports] == [stored.id]
    assert deps.repos.runs.get("alice", run.id).state == RunState.COMPLETE


def test_in_process_failure_after_the_report_is_stored_is_complete_not_failed(make_deps):
    deps = make_deps()
    runs, real = deps.repos.runs, deps.repos.runs.set_state

    def flaky(owner, run_id, state):
        if state == RunState.COMPLETE and not getattr(flaky, "done", False):
            flaky.done = True
            raise RuntimeError("database hiccup")
        return real(owner, run_id, state)

    runs.set_state = flaky
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert len(deps.repos.reports.list_for_owner("alice")) == 1
    assert not any(e.type == "run_failed" for e in events(deps, run))
