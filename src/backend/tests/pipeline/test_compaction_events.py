"""Issue #60: a real DeepAgents compaction is recorded once, in its own Run's log.

Nothing here fakes a compaction. The scripted models carry a small context-window `profile`,
which is how deepagents sets its own summarization trigger (85% of `max_input_tokens`), so the
production `SummarizationMiddleware` that `create_deep_agent` installs compacts for real: it
calls the model for a summary and records it in the agent's state. Production models keep
their real profiles and thresholds."""

import json

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.graph import run_brief
from tests.pipeline import fakes
from tests.scripted import ScriptedChat, call

BRIEF = "Compare BYD and Tesla in electric vehicles: sales, pricing, battery technology."
SUMMARY_PROMPT = "Context Extraction Assistant"  # LangChain's DEFAULT_SUMMARY_PROMPT role
FILLER = "Notes on the plan and its Sub-tasks. " * 450  # about 3,600 tokens per reply
WINDOW = 12_000  # the DeepAgents system prompt and tools fit; a few padded turns do not


def compacting(script: list[AIMessage], window: int = WINDOW) -> ScriptedChat:
    """Replays `script` with every reply padded, and answers the summarization middleware's
    own summary request, which arrives on the same model."""
    position = {"next": 0}

    def reply(messages) -> AIMessage:
        if any(SUMMARY_PROMPT in str(m.content) for m in messages):
            return AIMessage(content="Summary: a plan of three Sub-tasks was submitted.")
        message = script[position["next"]]
        position["next"] += 1
        return AIMessage(content=f"{message.content} {FILLER}", tool_calls=message.tool_calls)

    return ScriptedChat(reply=reply, profile={"max_input_tokens": window})


def summary_calls(model: ScriptedChat) -> int:
    return sum(any(SUMMARY_PROMPT in str(m.content) for m in conv) for conv in model.seen)


def planner_calls(model: ScriptedChat) -> int:
    return len(model.seen) - summary_calls(model)


def compactions(deps, run) -> list[dict]:
    return [e.payload for e in deps.repos.events.read_after("alice", run.id, limit=10_000)
            if e.type == "context_compacted"]


def test_a_real_planner_compaction_is_recorded_in_the_run_log(make_deps):
    planner = compacting(fakes.planner().script)
    deps = make_deps(planner_model=planner)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert summary_calls(planner) >= 1  # the middleware really summarised
    recorded = compactions(deps, run)
    assert len(recorded) == summary_calls(planner)  # one event per real compaction
    assert all(e["agent"] == "planner" and e["subtask_id"] is None for e in recorded)
    assert all(e["cutoff_index"] > 0 for e in recorded)


def test_each_later_compaction_is_recorded_and_none_twice(make_deps):
    # The repair round lengthens the thread: it compacts again, and the Planner keeps being
    # called with the earlier compaction still in its state.
    planner = compacting(fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",)).script)
    deps = make_deps(planner_model=planner)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    compacted = summary_calls(planner)
    assert compacted >= 2
    assert len(planner.seen) - compacted > compacted  # more Planner calls than compactions
    cutoffs = [e["cutoff_index"] for e in compactions(deps, run)]
    assert len(cutoffs) == compacted
    assert cutoffs == sorted(set(cutoffs))  # a new, later cutoff each time


def test_a_restarted_run_keeps_its_compactions_and_records_new_ones_once(make_deps):
    import uuid

    from insightforge_agent.domain.models import Run
    from insightforge_agent.pipeline.graph import build_graph, outer_config

    script = fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",)).script
    first = compacting(script)
    deps = make_deps(planner_model=first)
    run = Run(id="run_cmp", owner_id="alice", brief=BRIEF, state=RunState.PLANNING,
              created_at=deps.now())
    deps.repos.runs.add(run)
    thread = f"thread-{uuid.uuid4()}"  # the Planner's; outer_config derives the graph's
    config = outer_config(thread)
    initial = {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": BRIEF}
    build_graph(deps, interrupt_after=["researching"]).invoke(initial, config)
    before = compactions(deps, run)
    assert len(before) == summary_calls(first) == 1

    # A fresh Deps, as after a restart: the same stores and checkpointer, a new Planner and
    # recorder, and a model that carries on with the rest of the script.
    second = compacting(script[planner_calls(first):])
    restarted = make_deps(repos=deps.repos, store=deps.store, checkpointer=deps.checkpointer,
                          planner_model=second)
    build_graph(restarted).invoke(None, config)
    assert restarted.repos.runs.get("alice", run.id).state == RunState.COMPLETE
    assert planner_calls(second) > summary_calls(second) >= 1  # it ran with the old event
    after = compactions(restarted, run)
    assert after[:1] == before  # the earlier compaction is not recorded again
    assert len(after) == 1 + summary_calls(second)
    assert [e["cutoff_index"] for e in after] == sorted({e["cutoff_index"] for e in after})



def test_a_restarted_run_does_not_record_an_earlier_compaction_again(make_deps):
    """After a restart the old compaction's event is still in the Planner's checkpointed state,
    and the new process's recorder sees it on every call; it must not be logged twice."""
    import uuid

    from insightforge_agent.domain.models import Run
    from insightforge_agent.pipeline.graph import build_graph, outer_config

    script = fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",)).script
    first = compacting(script)
    deps = make_deps(planner_model=first)
    run = Run(id="run_cmp2", owner_id="alice", brief=BRIEF, state=RunState.PLANNING,
              created_at=deps.now())
    deps.repos.runs.add(run)
    thread = f"thread-{uuid.uuid4()}"
    config = outer_config(thread)
    initial = {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": BRIEF}
    build_graph(deps, interrupt_after=["researching"]).invoke(initial, config)
    before = compactions(deps, run)
    assert len(before) == 1

    # A wider window after the restart: nothing new compacts, the old event stays in force.
    second = compacting(script[planner_calls(first):], window=200_000)
    restarted = make_deps(repos=deps.repos, store=deps.store, checkpointer=deps.checkpointer,
                          planner_model=second)
    build_graph(restarted).invoke(None, config)
    assert restarted.repos.runs.get("alice", run.id).state == RunState.COMPLETE
    assert planner_calls(second) >= 2 and summary_calls(second) == 0
    assert compactions(restarted, run) == before

def compacting_researcher(window: int = WINDOW) -> ScriptedChat:
    """The scripted Researcher, padded and searching twice, so its own conversation compacts:
    the real middleware keeps the second search and summarises the first."""
    base = fakes.researcher().reply

    def reply(messages) -> AIMessage:
        if any(SUMMARY_PROMPT in str(m.content) for m in messages):
            return AIMessage(content="Summary: the first search was run.")
        compacted = any(isinstance(m, HumanMessage) and "Summary:" in str(m.content)
                        for m in messages)
        searches = sum(isinstance(m, ToolMessage) for m in messages)
        if searches == 1 and not compacted:
            task = str(next(m.content for m in messages if isinstance(m, HumanMessage)))
            query = task.split(" ", 1)[1].strip()
            return AIMessage(content=FILLER, tool_calls=[
                call("web_search", {"query": query}, f"again-{abs(hash(task))}")])
        message = base(messages)
        return AIMessage(content=f"{message.content} {FILLER}", tool_calls=message.tool_calls)

    return ScriptedChat(reply=reply, profile={"max_input_tokens": window})


def test_a_subagent_compaction_is_attributed_to_its_agent_and_subtask(make_deps):
    researcher = compacting_researcher()
    deps = make_deps(researcher_model=researcher)  # the Planner keeps its unbounded fake
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert summary_calls(researcher) == 3  # each of s1, s2 and s3 compacted once
    recorded = compactions(deps, run)
    assert sorted((e["agent"], e["subtask_id"]) for e in recorded) == [
        ("researcher", "s1"), ("researcher", "s2"), ("researcher", "s3")]
    assert len({e["compaction_id"] for e in recorded}) == 3


def test_without_a_small_context_window_nothing_is_compacted_or_recorded(make_deps):
    deps = make_deps()  # the ordinary fakes carry no profile: deepagents' 170,000-token trigger
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert compactions(deps, run) == []


def test_compaction_events_are_durable_in_the_sqlite_run_log(tmp_path):
    """Also the README's evidence: run with `-s` to print the persisted Run log."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from insightforge_agent.pipeline.deps import Deps
    from insightforge_agent.stores.source_store import SourceStore
    from insightforge_agent.stores.sqlite import SqliteRepos

    planner = compacting(fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",)).script)
    repos = SqliteRepos(str(tmp_path / "app.db"))
    with SqliteSaver.from_conn_string(str(tmp_path / "ck.db")) as saver:
        search = fakes.FixtureSearch()
        deps = Deps(repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
                    planner_model=planner, writer_model=fakes.writer(), light_model=fakes.light(),
                    researcher_model=fakes.researcher(), search=search, fetcher=search,
                    checkpointer=saver)
        run = run_brief(deps, "alice", Brief(text=BRIEF))
    repos.close()
    assert run.state == RunState.COMPLETE

    reopened = SqliteRepos(str(tmp_path / "app.db"))  # a new connection to the same file
    try:
        log = reopened.events.read_after("alice", run.id, limit=10_000)
        assert reopened.events.read_after("bob", run.id) == []  # owner-scoped like every event
    finally:
        reopened.close()
    print(f"\nrun_id={run.id} state={run.state.value} planner_model_calls="
          f"{planner_calls(planner)} summary_calls={summary_calls(planner)}")
    for e in log:
        if e.type in ("stage", "repair", "context_compacted"):
            print(f"seq={e.seq} {e.type} {json.dumps(e.payload, sort_keys=True)}")
    stored = [e for e in log if e.type == "context_compacted"]
    assert len(stored) == summary_calls(planner) == 2
    assert [e.payload["cutoff_index"] for e in stored] == [4, 10]
    assert all(e.payload["agent"] == "planner" for e in stored)
    repair = next(e.seq for e in log if e.type == "repair")
    assert stored[0].seq < repair < stored[1].seq  # one before the repair round, one after


def test_a_compaction_that_leaves_the_cutoff_unchanged_still_has_its_own_id():
    # deepagents stores prior_cutoff + effective_cutoff - 1, so a compaction that summarises
    # only the previous summary (effective cutoff 1) keeps the same cutoff_index. It is still
    # a new compaction, with a new summary, and must not be taken for the earlier one.
    from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY

    from insightforge_agent.agents.compaction import compaction_of

    def state(summary: str) -> dict:
        return {"messages": [HumanMessage("PLAN", id="m0"), AIMessage("ok", id="m1")],
                SUMMARIZATION_EVENT_KEY: {"cutoff_index": 4, "file_path": None,
                                          "summary_message": HumanMessage(summary)}}

    first, again, later = (compaction_of("planner", state(s))
                           for s in ("Summary one.", "Summary one.", "Summary two."))
    assert first.compaction_id == again.compaction_id  # the same event seen twice
    assert later.compaction_id != first.compaction_id  # a new compaction at the same cutoff


@pytest.mark.parametrize("resumed_window, new_compactions", [
    (WINDOW, 1),  # the resumed Planner compacts again: only that one is added
    (200_000, 0),  # nothing new compacts: the old event stays in force on every call
])
def test_a_run_resumed_from_reopened_sqlite_files_records_each_compaction_once(
        tmp_path, resumed_window, new_compactions):
    """A real process restart: every SQLite connection (repositories and checkpointer) is
    closed after the first compaction is persisted, then reopened from disk to resume."""
    import uuid

    from langgraph.checkpoint.sqlite import SqliteSaver

    from insightforge_agent.domain.models import Run
    from insightforge_agent.pipeline.deps import Deps
    from insightforge_agent.pipeline.graph import build_graph, outer_config
    from insightforge_agent.stores.source_store import SourceStore
    from insightforge_agent.stores.sqlite import SqliteRepos

    def process(planner: ScriptedChat, repos: SqliteRepos, saver) -> Deps:
        search = fakes.FixtureSearch()
        return Deps(repos=repos, store=SourceStore(repos.sources, repos.observations, repos.passages),
                    planner_model=planner, writer_model=fakes.writer(), light_model=fakes.light(),
                    researcher_model=fakes.researcher(), search=search, fetcher=search,
                    checkpointer=saver)

    app_db, ck_db = str(tmp_path / "app.db"), str(tmp_path / "ck.db")
    script = fakes.planner(research_ids=("s1", "s2"), repair_ids=("s3",)).script
    thread = f"thread-{uuid.uuid4()}"
    config = outer_config(thread)

    first = compacting(script)
    repos = SqliteRepos(app_db)
    with SqliteSaver.from_conn_string(ck_db) as saver:
        deps = process(first, repos, saver)
        run = Run(id="run_reopen", owner_id="alice", brief=BRIEF, state=RunState.PLANNING,
                  created_at=deps.now())
        repos.runs.add(run)
        build_graph(deps, interrupt_after=["researching"]).invoke(
            {"thread_id": thread, "owner_id": "alice", "run_id": run.id, "brief": BRIEF}, config)
        before = compactions(deps, run)
    repos.close()
    assert len(before) == summary_calls(first) == 1

    second = compacting(script[planner_calls(first):], window=resumed_window)
    repos = SqliteRepos(app_db)  # reopened from disk, with the checkpoint restored from ck.db
    try:
        with SqliteSaver.from_conn_string(ck_db) as saver:
            resumed = process(second, repos, saver)
            build_graph(resumed).invoke(None, config)
            after = compactions(resumed, run)
            assert repos.runs.get("alice", run.id).state == RunState.COMPLETE
    finally:
        repos.close()
    assert planner_calls(second) >= 2  # the resumed Planner ran with the old event in state
    assert summary_calls(second) == new_compactions
    assert after[:1] == before  # the persisted compaction is not recorded again
    assert len(after) == 1 + new_compactions
    assert len({e["compaction_id"] for e in after}) == len(after)
