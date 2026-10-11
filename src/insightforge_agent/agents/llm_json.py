"""One bounded model call that must answer with JSON matching a pydantic model."""

import re
from dataclasses import dataclass
from typing import Literal, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

# A failed reply is kept only as this much of its start and end (F3): enough to see where it
# stopped, never the whole reply or the Source text it quotes.
HEAD_CHARS = 200
TAIL_CHARS = 200
_ERROR_CHARS = 120
_MAX_ERROR_TYPES = 5

Kind = Literal["empty", "incomplete_json", "invalid_json", "schema"]


@dataclass(frozen=True)
class ReplyDiagnostic:
    """Why a reply was not the JSON asked for. `incomplete_json` means the JSON stopped before
    it closed; whether the provider cut it off is told by `stop_reason`, not by the text."""

    schema: str
    kind: Kind
    error_count: int
    error_types: tuple[str, ...]  # distinct, in order of first appearance
    error: str  # the first problem's message, bounded; never the reply itself
    length: int  # characters of reply text
    head: str
    tail: str  # what follows `head`, at most TAIL_CHARS; empty if the head was all of it
    stop_reason: str | None  # as the provider reported it, under either common name
    output_tokens: int | None
    reasoning_tokens: int | None  # part of output_tokens, where the provider separates them
    max_tokens: int | None  # the model's configured output ceiling, if it exposes one

    def render(self) -> str:
        def reported(v: object) -> str:
            return "not reported" if v is None else str(v)

        tokens = reported(self.output_tokens)
        if self.reasoning_tokens is not None:
            tokens += f" (reasoning {self.reasoning_tokens})"
        text = f"head={self.head!r}" + (f"; tail={self.tail!r}" if self.tail else "")
        return (f"{self.schema}: {self.kind}, {self.error_count} problem(s) "
                f"[{', '.join(self.error_types)}: {self.error}]; "
                f"stop_reason={reported(self.stop_reason)}; output_tokens={tokens}; "
                f"max_tokens={reported(self.max_tokens)}; {self.length} chars; {text}")


class BadModelReply(ValueError):
    """The model's reply was not the JSON that was asked for. `str()` is the bounded
    diagnostic for logs; `brief` is the short form fed back to a model."""

    def __init__(self, diagnostic: ReplyDiagnostic, brief: str):
        super().__init__(diagnostic.render())
        self.diagnostic = diagnostic
        self.brief = brief


def ask_json(model: BaseChatModel, system: str, user: str, schema: type[T]) -> T:
    reply = model.invoke([SystemMessage(system), HumanMessage(user)])
    text = _FENCE.sub("", reply.text.strip())
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        # Copied out so that nothing holding the whole reply outlives this block.
        errors = e.errors(include_url=False, include_input=False, include_context=False)
        count = e.error_count()
    # Raised outside the except block: no chained ValidationError keeps the reply alive.
    raise BadModelReply(_diagnose(reply, model, schema.__name__, text, errors, count),
                        f"{schema.__name__}: {count} problem(s) in {text[:200]!r}")


def _diagnose(reply: BaseMessage, model: BaseChatModel, schema: str, parsed: str,
              errors: list, count: int) -> ReplyDiagnostic:
    raw = reply.text
    types = tuple(dict.fromkeys(str(err["type"]) for err in errors))[:_MAX_ERROR_TYPES]
    first = str(errors[0]["msg"]) if errors else ""
    if not parsed:
        kind: Kind = "empty"
    elif "json_invalid" in types:
        kind = "incomplete_json" if "EOF while parsing" in first else "invalid_json"
    else:
        kind = "schema"
    # Provider-neutral: LangChain merges the provider's finish information into
    # response_metadata (Anthropic: stop_reason; Gemini, Groq: finish_reason) and its
    # token counts into usage_metadata.
    meta = reply.response_metadata or {}
    stop = meta.get("stop_reason") or meta.get("finish_reason")
    usage = getattr(reply, "usage_metadata", None) or {}
    details = usage.get("output_token_details") or {}
    ceiling = getattr(model, "max_tokens", None)
    return ReplyDiagnostic(
        schema=schema, kind=kind, error_count=count, error_types=types,
        error=first[:_ERROR_CHARS], length=len(raw),
        head=raw[:HEAD_CHARS], tail=raw[max(HEAD_CHARS, len(raw) - TAIL_CHARS):],
        stop_reason=None if stop is None else str(stop)[:40],
        output_tokens=_int(usage.get("output_tokens")),
        reasoning_tokens=_int(details.get("reasoning")),
        max_tokens=_int(ceiling))


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None
