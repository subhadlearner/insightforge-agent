"""Scripted chat models: deterministic stand-ins for the Planner and Researcher."""

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChat(BaseChatModel):
    """Replays `script` in order, or answers each call with `reply(messages)`.
    Records every conversation it is given in `seen`."""

    script: list[AIMessage] = Field(default_factory=list)
    reply: Callable[[list[BaseMessage]], AIMessage] | None = None
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    position: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if self.reply is not None:
            message = self.reply(list(messages))
        else:
            if self.position >= len(self.script):
                raise AssertionError("scripted model called more times than scripted")
            message = self.script[self.position]
            self.position += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def ai(*calls: dict, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))


SUPPORT_PREFIX = "You check whether"  # pipeline/support.py: the ADR-0006 support judgment


def support_verdicts(user: str, verdict=lambda statement, text: "SUPPORTED",
                     present: bool = False) -> list[dict]:
    """A scripted ADR-0006 support reply: `verdict(statement, passage_text)` per numbered
    passage, each SUPPORTED one with the permissive 'full' support map (the whole statement as
    one part, the whole shown passage as its excerpt), so code's checks still apply."""
    import re as _re

    statement = user.split("\n", 1)[0].removeprefix("Statement: ")
    out = []
    for n, text in _re.findall(r"^\[(\d+)\] (.+)$", user, _re.MULTILINE):
        v = verdict(statement, text)
        out.append({"passage": int(n), "verdict": v, "asserts_present_state": present,
                    **({"parts": [{"part": statement, "excerpts": [text]}]}
                       if v == "SUPPORTED" else {})})
    return out
