"""Scripted chat models: deterministic stand-ins for the Planner and Researcher."""

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChat(BaseChatModel):
    """Replays `script` in order, or answers each call with `reply(messages)`.
    Records every conversation it is given in `seen`. With `reply`, the final-review checks
    (coverage, answer verifier, shortening) get `check_reply`'s permissive default unless
    `checks` is False, so a test scripts them only when it is about them."""

    script: list[AIMessage] = Field(default_factory=list)
    reply: Callable[[list[BaseMessage]], AIMessage] | None = None
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    position: int = 0
    checks: bool = True

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChat":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        default = (check_reply(str(messages[0].content), str(messages[-1].content))
                   if self.reply is not None and self.checks and len(messages) > 1 else None)
        if default is not None:
            message = AIMessage(content=default)
        elif self.reply is not None:
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
JOINT_PREFIX = "You check whether numbered passages, taken together"  # judge_joint
ANSWERS_PREFIX = "You check which numbered evidence statements answer"  # confirm_answers
COVERAGE_PREFIX = "You list what a text asks"  # check_coverage (Brief and question)
VERIFY_PREFIX = "You verify claimed answers"  # confirm_answers' independent verifier
SHORTEN_PREFIX = "You shorten one evidence statement"  # synthesize._shortened


def coverage_verdict(user: str, uncovered: tuple[str, ...] = ()) -> dict:
    """A scripted coverage check: the whole text as one ask covered by every item, plus each of
    `uncovered` (words of the text) as an ask no item covers."""
    import re as _re

    text = user.split("\n\nItems:\n", 1)[0].split(": ", 1)[1]
    items = [int(n) for n in _re.findall(r"^\[(\d+)\] ", user, _re.MULTILINE)]
    return {"asks": [{"quote": text, "covered_by": items}]
            + [{"quote": q, "covered_by": []} for q in uncovered]}


def verify_verdicts(user: str, verdict=lambda part, statement: "ANSWERS") -> dict:
    """A scripted answer verifier: `verdict(part, statement)` per numbered pair."""
    import re as _re

    pairs = _re.findall(r"^\[(\d+)\] Asked: (.+)\nStatement: (.+)$", user, _re.MULTILINE)
    return {"verdicts": [{"pair": int(n), "verdict": verdict(p, s)} for n, p, s in pairs]}


def check_reply(system: str, user: str) -> str | None:
    """The default scripted reply to the final-review checks (coverage, answer verifier,
    shortening), or None for any other call. Shortening returns the statement unchanged, so
    nothing is shortened unless a test scripts it."""
    import json as _json

    if system.startswith(COVERAGE_PREFIX):
        return _json.dumps(coverage_verdict(user))
    if system.startswith(VERIFY_PREFIX):
        return _json.dumps(verify_verdicts(user))
    if system.startswith(SHORTEN_PREFIX):
        return _json.dumps({"statement": user.removeprefix("Statement: ")})
    return None


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


def joint_verdict(user: str, verdict=lambda statement, texts: "SUPPORTED") -> dict:
    """A scripted joint support reply (`judge_joint`, also the summary check): SUPPORTED with
    the permissive map (the whole statement as one part, every shown unit as its excerpts)."""
    import re as _re

    statement = user.split("\n", 1)[0].removeprefix("Statement: ")
    units = _re.findall(r"^\[(\d+)\] (.+)$", user, _re.MULTILINE)
    v = verdict(statement, [t for _, t in units])
    return {"verdict": v, "parts": [{"part": statement, "excerpts": [
        {"passage": int(n), "text": t} for n, t in units]}] if v == "SUPPORTED" else []}


def answer_verdicts(user: str, answers=lambda question, statement: True) -> dict:
    """A scripted requirement answer check (`confirm_answers`): the whole question as its one
    part, and `answers(question, statement)` per numbered statement, quoting the whole statement
    when it states that part."""
    import re as _re

    question = user.split("\n", 1)[0].removeprefix("Question: ")
    out = []
    for n, t in _re.findall(r"^\[(\d+)\] (.+)$", user, _re.MULTILINE):
        yes = answers(question, t)
        out.append({"statement": int(n), "parts": [1] if yes else [], "excerpt": t if yes else ""})
    return {"parts": [question.rstrip("?")], "answers": out}
