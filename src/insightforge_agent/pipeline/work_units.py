"""Model work units with bounded recovery (ADR-0006 §4).

A work unit is one model call over a batch of Passage windows. Its outcome is logged as a
`work_unit` event: `parsed`, or `truncated` / `invalid` / `rejected` followed by `split` (the
batch is retried as two halves) or `exhausted` (its windows are reported as unread). A cut-off
or malformed reply is therefore never indistinguishable from "nothing relevant here".

Bounds: a batch is split only when it holds more than one Passage, at most `MAX_SPLIT_DEPTH`
levels deep, and only while the Run's retry allowance (`Deps.work_unit_retry_cap`) lasts. A
half that parses is kept and never redone; only the failing half is split again. Every call is
counted against the Run's token cap by the caller's `call`, as before."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from insightforge_agent.agents.llm_json import BadModelReply, ReplyDiagnostic, StructuredOutputRejected
from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.domain.passages import Window
from insightforge_agent.pipeline.deps import Deps

MAX_SPLIT_DEPTH = 2
R = TypeVar("R")


def truncated(d: ReplyDiagnostic) -> bool:
    """Whether a reply stopped at the output limit: the provider said so, or the JSON ends
    before it closes."""
    return (d.stop_reason or "").lower() in ("max_tokens", "length") or d.kind == "incomplete_json"


def refs_of(windows: list[Window]) -> list[PassageRef]:
    return list(dict.fromkeys(PassageRef(observation_id=w.passage.observation_id,
                                         index=w.passage.index) for w in windows))


def halves(windows: list[Window]) -> tuple[list[Window], list[Window]]:
    """Two halves by Passage: the windows of one Passage stay together."""
    refs = refs_of(windows)
    left = set(refs[:len(refs) // 2])

    def on_left(w: Window) -> bool:
        return PassageRef(observation_id=w.passage.observation_id, index=w.passage.index) in left

    return [w for w in windows if on_left(w)], [w for w in windows if not on_left(w)]


@dataclass
class Outcome(Generic[R]):
    windows: list[Window]
    result: R | None  # None when exhausted
    reason: str = ""  # why it was exhausted


def run_unit(deps: Deps, owner_id: str, run_id: str, windows: list[Window],
             call: Callable[[list[Window]], R], *, stage: str, unit: str, depth: int = 0,
             **where) -> list[Outcome[R]]:
    """`call` over `windows`, recovering a truncated, invalid or rejected reply by splitting.
    Returns one Outcome per final unit, in order."""
    refs = [r.model_dump() for r in refs_of(windows)]
    base = dict(stage=stage, unit=unit, depth=depth, passages=refs, **where)
    try:
        result = call(windows)
    except (BadModelReply, StructuredOutputRejected) as e:
        if isinstance(e, BadModelReply):
            reason = "truncated" if truncated(e.diagnostic) else "invalid"
            detail = {"kind": e.diagnostic.kind, "stop_reason": e.diagnostic.stop_reason,
                      "output_tokens": e.diagnostic.output_tokens,
                      "max_tokens": e.diagnostic.max_tokens,
                      "detail": str(e)}  # the bounded F3 diagnostic, never the whole reply
        else:
            reason, detail = "rejected", {"error": str(e)[:200]}
        deps.log(owner_id, run_id, "work_unit", **base, status=reason, **detail)
        if len(refs) > 1 and depth < MAX_SPLIT_DEPTH and deps.take_retry(owner_id, run_id):
            deps.log(owner_id, run_id, "work_unit", **base, status="split")
            left, right = halves(windows)
            return [*run_unit(deps, owner_id, run_id, left, call, stage=stage,
                              unit=f"{unit}.a", depth=depth + 1, **where),
                    *run_unit(deps, owner_id, run_id, right, call, stage=stage,
                              unit=f"{unit}.b", depth=depth + 1, **where)]
        deps.log(owner_id, run_id, "work_unit", **base, status="exhausted", reason=reason)
        return [Outcome(windows, None, reason)]
    deps.log(owner_id, run_id, "work_unit", **base, status="parsed")
    return [Outcome(windows, result)]
