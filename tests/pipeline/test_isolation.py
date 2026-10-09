"""T5: Researchers are isolated (ADR-0002, design.md section 4), and Extraction's output is stored."""

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from insightforge_agent.agents.researcher import make_web_search_tool, researcher_subagent
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.graph import run_brief
from tests.pipeline import fakes
from tests.pipeline.test_happy_path import BRIEF


def test_the_researcher_subagent_has_its_own_tools_and_is_not_forked(make_deps):
    deps = make_deps()
    tool = make_web_search_tool(
        owner_id="alice", run_id="r", search=deps.search, fetcher=deps.fetcher, store=deps.store,
        summarizer=deps.light_model, events=deps.repos.events, now=deps.now,
        passage_token_cap=deps.passage_token_cap)
    definition = researcher_subagent([tool], deps.researcher_model)
    assert [t.name for t in definition["tools"]] == ["web_search"]
    assert definition["model"] is deps.researcher_model
    assert "fork" not in str(definition.get("mode", "")).lower()


def test_each_researcher_receives_only_its_own_subtask(make_deps):
    researcher = fakes.researcher()
    deps = make_deps(researcher_model=researcher)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    assert researcher.seen
    for conversation in researcher.seen:
        human = [m for m in conversation if isinstance(m, HumanMessage)]
        assert len(human) == 1  # the task description and nothing else
        own = [sid for sid in fakes.QUERIES if f"SUBTASK_ID={sid} " in str(human[0].content)]
        assert len(own) == 1
        everything = "\n".join(str(m.content) for m in conversation
                               if not isinstance(m, (SystemMessage, ToolMessage)))
        assert BRIEF not in everything
        for sid, query in fakes.QUERIES.items():
            if sid != own[0]:
                assert query not in everything
        # what a tool hands back is only {source_id, title, summary}
        for m in conversation:
            if isinstance(m, ToolMessage):
                assert "4.27 million" not in str(m.content)


def test_extracted_entities_and_facts_are_stored_with_passage_references(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    stored = deps.repos.entities.list_for_run("alice", run.id)
    assert stored
    passages = {(o.id, p.index)
                for s in deps.repos.sources.list_for_run("alice", run.id)
                for o in deps.repos.observations.list_for_source("alice", s.id)
                for p in deps.repos.passages.list("alice", o.id)}
    assert all((f.passage.observation_id, f.passage.index) in passages for f in stored)
    assert deps.repos.entities.list_for_run("bob", run.id) == []
