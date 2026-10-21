"""ADR-0006 §4: model work units with bounded recovery, in Extraction.

T18 Stage C's regulatory Run lost 5 drafting and 2 extraction batches cut at 4,096 output tokens
and 1 invalid JSON extraction reply, each logged and returned as an empty batch. Here a scripted
model cuts off or garbles any batch larger than it can answer: the batch is split and retried
within the bounds, what parses is kept, and what stays unread is reported, never read as "no
facts". Scripted models only: the provider's real output limits are not exercised."""

import json
import re

from langchain_core.messages import AIMessage

from insightforge_agent.domain.contracts import ResearchResults, SourceRef, SubTaskResult
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.write import research_gaps
from insightforge_agent.domain.contracts import EvidenceBundle
from tests.pipeline.test_extraction_synthesis import OWNER, RUN, Light, world  # noqa: F401

BODY = "\n\n".join(f"BYD sold {n}.5 million vehicles in 2025." for n in range(1, 9))  # 8 Passages


def limited(max_passages: int, failure: str = "truncated") -> Light:
    """Extracts one fact per Passage, but a batch of more than `max_passages` Passages fails:
    cut off at the output limit, or answered with invalid JSON."""
    def reply(messages):
        user = str(messages[-1].content)
        shown = re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE)
        if len(shown) > max_passages:
            if failure == "truncated":
                return AIMessage(content='{"facts": [{"passage_index": 0, "statement": "BYD so',
                                 response_metadata={"stop_reason": "max_tokens"})
            return AIMessage(content='{"facts": [oops]}')
        return AIMessage(content=json.dumps({"facts": [
            {"passage_index": int(n), "statement": t} for n, t in shown]}))
    return Light(reply=reply)


def setup(world, model, **deps):
    sid = world.page("https://a.test/x", BODY)
    world.deps.light_model = model
    for k, v in deps.items():
        setattr(world.deps, k, v)
    return sid


def statuses(world):
    return [(e["unit"], e["status"]) for e in world.events("work_unit")]


def test_a_truncated_batch_is_split_and_every_fact_is_kept(world):
    sid = setup(world, limited(4))
    result = extract(world.deps, OWNER, RUN, [sid])
    assert len(result.facts) == 8 and result.failed_units == []
    assert statuses(world) == [
        (f"{sid}:1", "truncated"), (f"{sid}:1", "split"),
        (f"{sid}:1.a", "parsed"), (f"{sid}:1.b", "parsed")]
    (cut,) = [e for e in world.events("work_unit") if e["status"] == "truncated"]
    assert cut["stop_reason"] == "max_tokens" and cut["kind"] == "incomplete_json"
    assert len(cut["passages"]) == 8


def test_a_half_that_parses_is_never_redone(world):
    sid = setup(world, limited(3))  # 8 -> 4 + 4 -> each 4 -> 2 + 2
    result = extract(world.deps, OWNER, RUN, [sid])
    assert len(result.facts) == 8
    parsed = [u for u, s in statuses(world) if s == "parsed"]
    assert parsed == [f"{sid}:1.a.a", f"{sid}:1.a.b", f"{sid}:1.b.a", f"{sid}:1.b.b"]
    assert len(world.deps.light_model.seen) == 7  # 1 + 2 + 4 calls, nothing repeated


def test_past_the_depth_bound_the_unread_passages_are_reported_not_dropped(world):
    sid = setup(world, limited(1, failure="invalid"))  # nothing above one Passage parses
    result = extract(world.deps, OWNER, RUN, [sid])
    # Depth 2: 8 -> 4 + 4 -> 2 + 2 + 2 + 2, each exhausted; no unbounded retrying.
    assert len(world.deps.light_model.seen) == 7 and result.facts == []
    assert [len(u.passages) for u in result.failed_units] == [2, 2, 2, 2]
    assert {u.reason for u in result.failed_units} == {"invalid"}
    assert sorted(r.index for u in result.failed_units for r in u.passages) == list(range(8))
    (done,) = world.events("extraction_source_done")
    assert len(done["failed_units"]) == 4


def test_the_runs_retry_allowance_bounds_recovery(world):
    sid = setup(world, limited(1), work_unit_retry_cap=1)
    result = extract(world.deps, OWNER, RUN, [sid])
    assert len(world.deps.light_model.seen) == 3  # the batch, then its two halves only
    assert [len(u.passages) for u in result.failed_units] == [4, 4]


def test_a_single_passage_batch_is_not_split(world):
    sid = world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.")
    world.deps.light_model = limited(0)
    result = extract(world.deps, OWNER, RUN, [sid])
    assert len(world.deps.light_model.seen) == 1
    assert statuses(world) == [(f"{sid}:1", "truncated"), (f"{sid}:1", "exhausted")]
    (lost,) = result.failed_units
    assert lost.reason == "truncated" and len(lost.passages) == 1


def test_unread_passages_become_a_gap_line_whatever_else_the_subtask_kept(world):
    sid = setup(world, limited(1, failure="invalid"), work_unit_retry_cap=1)
    extraction = extract(world.deps, OWNER, RUN, [sid])
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Sales")], sub_tasks=[
        SubTask(id="s1", aspect_id="a1", query="BYD sales 2025", source_type="web",
                time_horizon_months=12, priority=1)])
    research = ResearchResults(results=[SubTaskResult(subtask_id="s1", status="succeeded",
                                                      source_refs=[SourceRef(source_id=sid,
                                                                             title="x",
                                                                             summary="s")])])
    lines = research_gaps(plan=plan, research=research, bundle=EvidenceBundle(sections=[]),
                          events=[], documents=[], extraction=extraction)
    assert ("Sub-task s1 (BYD sales 2025): 8 Passage(s) in 2 extraction batch(es) could not be "
            "read after bounded retries (invalid); facts they may state were not considered."
            ) in lines


def test_a_resumed_extraction_never_asks_again_for_a_source_it_finished(world):
    sid = setup(world, limited(8))
    first = extract(world.deps, OWNER, RUN, [sid])
    calls = len(world.deps.light_model.seen)
    again = extract(world.deps, OWNER, RUN, [sid])
    assert len(world.deps.light_model.seen) == calls
    assert [f.id for f in again.facts] == [f.id for f in first.facts]


def test_a_newer_observation_of_the_source_is_extracted_afresh(world):
    sid = setup(world, limited(8))
    extract(world.deps, OWNER, RUN, [sid])
    calls = len(world.deps.light_model.seen)
    world.page("https://a.test/x", "BYD sold 9.5 million vehicles in 2026.")  # refetched
    again = extract(world.deps, OWNER, RUN, [sid])
    assert len(world.deps.light_model.seen) == calls + 1
    assert [f.statement for f in again.facts] == ["BYD sold 9.5 million vehicles in 2026."]
