"""Spike facts about the installed deepagents version (design.md section 12)."""

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models.fake_chat_models import FakeListChatModel


def _tools(**kwargs) -> set[str]:
    agent = create_deep_agent(model=FakeListChatModel(responses=["ok"]), **kwargs)
    return set(agent.nodes["tools"].bound.tools_by_name)


def test_write_todos_is_not_on_by_default():
    assert "write_todos" not in _tools()
    assert "task" in _tools()


def test_write_todos_available_when_middleware_passed_explicitly():
    assert "write_todos" in _tools(middleware=[TodoListMiddleware()])


def test_the_summarization_event_contract_the_compaction_recorder_reads():
    # agents/compaction.py reads this state key and these fields (issue #60). The key is
    # private to deepagents, so an upgrade that renames or reshapes it must fail here rather
    # than silently stop the Run log recording compactions. The end-to-end proof that the
    # real middleware fills it is tests/pipeline/test_compaction_events.py.
    from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY, SummarizationEvent

    assert SUMMARIZATION_EVENT_KEY == "_summarization_event"
    assert set(SummarizationEvent.__annotations__) == {"cutoff_index", "summary_message",
                                                        "file_path"}
