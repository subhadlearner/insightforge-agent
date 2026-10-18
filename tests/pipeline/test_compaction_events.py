"""Issue #60: a real DeepAgents compaction is recorded once, in its own Run's log.

Nothing here fakes a compaction. The scripted models carry a small context-window `profile`,
which is how deepagents sets its own summarization trigger (85% of `max_input_tokens`), so the
production `SummarizationMiddleware` that `create_deep_agent` installs compacts for real: it
calls the model for a summary and records it in the agent's state. Production models keep
their real profiles and thresholds."""

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


def planner_calls(model: ScriptedChat) -> int:
    return len(model.seen) - summary_calls(model)


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
    from langgraph.checkpoint.sqlite import SqliteSaver

    from insightforge_agent.pipeline.deps import Deps
    from insightforge_agent.stores.source_store import SourceStore
    from insightforge_agent.stores.sqlite import SqliteRepos

    planner = compacting(fakes.planner().script)
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
        stored = [e for e in reopened.events.read_after("alice", run.id, limit=10_000)
                  if e.type == "context_compacted"]
        assert len(stored) == summary_calls(planner) == 1
        assert stored[0].payload["agent"] == "planner" and stored[0].seq > 0
        assert reopened.events.read_after("bob", run.id) == []  # owner-scoped like every event
    finally:
        reopened.close()
