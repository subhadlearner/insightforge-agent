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
