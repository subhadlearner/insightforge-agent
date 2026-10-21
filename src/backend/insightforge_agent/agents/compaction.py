"""Records each context compaction DeepAgents performs, so the Run log can show it (issue #60).

`create_deep_agent` installs its `SummarizationMiddleware` on the Planner and on every
subagent. When a model request crosses the trigger, that middleware summarises the older
messages, calls the model with the summary in their place, and stores a `SummarizationEvent`
(`cutoff_index`, `summary_message`, `file_path`) under `SUMMARIZATION_EVENT_KEY` through the
model node's state update. Each compaction replaces the previous event with one holding a new
summary; `cutoff_index` counts the messages compacted so far, but stays the same when a
compaction summarises only the previous summary, so a compaction is identified by both.

`after_model` hooks run as their own graph node after the model node, so this one sees the
event of a compaction that has completed. LangChain strips a `wrap_model_call` command before
it reaches outer middleware, and deepagents 0.7.23 offers no compaction callback, so reading
the state through the library's exported key is the narrowest observation available. The key
itself is private to deepagents: `tests/test_deepagents_facts.py` pins that contract.

The event stays in state for every later model call of the conversation, and a resumed or
replayed conversation sees it again, so `on_compaction` must record each `compaction_id` once."""

import hashlib
from collections.abc import Callable

from deepagents.middleware.summarization import SUMMARIZATION_EVENT_KEY
from langchain.agents.middleware import AgentMiddleware
from pydantic import BaseModel

from insightforge_agent.domain.plan import PLANNER, dispatched_id

COMPACTION_EVENT = "context_compacted"


class ContextCompaction(BaseModel):
    """The `context_compacted` Run-log payload. No message text: only counts and references."""

    compaction_id: str  # one per compaction: agent, conversation, cutoff_index and summary
    agent: str  # "planner" or the subagent's name
    subtask_id: str | None  # the Sub-task a subagent was dispatched for; None for the Planner
    cutoff_index: int  # messages of this conversation compacted so far, this one included
    messages_in_conversation: int  # its full message history, which compaction leaves intact
    summary_chars: int
    history_file: str | None  # where deepagents offloaded the compacted messages (a path in the
    # agent's own checkpointed file state, StateBackend, not on disk); None if it could not


def compaction_of(agent: str, state) -> ContextCompaction | None:
    """The latest completed compaction of this conversation, or None if it has had none."""
    event = state.get(SUMMARIZATION_EVENT_KEY)
    messages = state.get("messages") or []
    if not event or not messages:
        return None
    first = messages[0]
    # The first message's id is assigned when the conversation starts and kept in its
    # checkpoint, so it names one Planner thread or one subagent dispatch.
    anchor = first.id or ""
    cutoff = int(event["cutoff_index"])
    summary = str(event["summary_message"].content)
    digest = hashlib.sha256(f"{agent}|{anchor}|{cutoff}|{summary}".encode()).hexdigest()[:16]
    return ContextCompaction(
        compaction_id=f"cmp_{digest}",
        agent=agent,
        subtask_id=None if agent == PLANNER else dispatched_id(str(first.content)),
        cutoff_index=cutoff,
        messages_in_conversation=len(messages),
        summary_chars=len(summary),
        history_file=event.get("file_path"),
    )


class CompactionRecorder(AgentMiddleware):
    """Hands each completed compaction of one agent's conversation to `on_compaction`."""

    def __init__(self, agent: str, on_compaction: Callable[[ContextCompaction], None]) -> None:
        super().__init__()
        self._agent = agent
        self._on_compaction = on_compaction

    @property
    def name(self) -> str:
        return f"CompactionRecorder[{self._agent}]"

    def after_model(self, state, runtime) -> None:
        if (compaction := compaction_of(self._agent, state)) is not None:
            self._on_compaction(compaction)
        return None
