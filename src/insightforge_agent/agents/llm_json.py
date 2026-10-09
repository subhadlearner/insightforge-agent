"""One bounded model call that must answer with JSON matching a pydantic model."""

import re
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class BadModelReply(ValueError):
    """The model's reply was not the JSON that was asked for."""


def ask_json(model: BaseChatModel, system: str, user: str, schema: type[T]) -> T:
    reply = model.invoke([SystemMessage(system), HumanMessage(user)])
    text = _FENCE.sub("", reply.text.strip())
    try:
        return schema.model_validate_json(text)
    except ValidationError as e:
        raise BadModelReply(f"{schema.__name__}: {e.error_count()} problem(s) in {text[:200]!r}") from e
