"""The Memory researcher: its search tool, prompt and subagent definition (design.md section 5).

The tool searches `report_sections` over the Reports the eligibility rule allows for this Run (the
User's own, from COMPLETE Runs, never the same Watchlist Item), fixed when the Run was submitted.
It hands the Memory researcher only references with the Report date, section heading and
relevance score, never a Report body. Each search is recorded as a durable `memory_search` event
naming the Sub-task that searched, so the graph accepts for a Sub-task only references that Sub-
task's own searches returned. What it finds is background, never current Evidence."""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from langchain.tools import ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from insightforge_agent.agents.doc_reader import searching_subtask
from insightforge_agent.domain.memory import memory_ref_id
from insightforge_agent.domain.models import RunEvent
from insightforge_agent.domain.plan import MEMORY_RESEARCHER
from insightforge_agent.domain.repositories import RunEventRepository
from insightforge_agent.stores.report_sections import DEFAULT_K, ReportSectionIndex

MEMORY_SEARCH_EVENT = "memory_search"  # durable record of {subtask_id, query, refs} a search returned

MEMORY_RESEARCHER_PROMPT = """You are a Memory Researcher. Your task description starts with
"SUBTASK_ID=<id>" followed by your research query. Call `memory_search` with that query (once
more with a refined query only if the first returns nothing useful). It returns Claims from the
user's earlier reports as {ref, report_date, heading, relevance_score}. They are background
from earlier reports, not current facts. Then reply with exactly one line:
"RESULT: <one sentence on what you found> | refs: <ref>, <ref>, ..."
listing only refs that `memory_search` returned and that bear on the query. If nothing useful
was found, reply "FAILED: <reason>"."""


def make_memory_search_tool(
    *,
    owner_id: str,
    run_id: str,
    index: ReportSectionIndex,
    report_ids: list[str],
    events: RunEventRepository,
    now: Callable[[], datetime],
    lock: AbstractContextManager,
    k: int = DEFAULT_K,
) -> BaseTool:
    """`report_ids` are the Reports this Run may search. `lock` serialises repository writes
    with every other Researcher tool of the Run; the vector search runs outside it."""
    allowed = list(report_ids)

    @tool
    def memory_search(query: str, runtime: ToolRuntime) -> str:
        """Search the user's earlier reports for the query and return the most relevant Claims
        as JSON {ref, report_date, heading, relevance_score}."""
        hits = index.search(owner_id, allowed, query, k)
        subtask_id = searching_subtask(runtime.state.get("messages", []))
        with lock:
            events.append(RunEvent(owner_id=owner_id, run_id=run_id, type=MEMORY_SEARCH_EVENT,
                                   payload={"subtask_id": subtask_id, "query": query[:500],
                                            "refs": [memory_ref_id(h.ref) for h in hits]},
                                   created_at=now()))
        return json.dumps([{"ref": memory_ref_id(h.ref), "report_date": h.report_date,
                            "heading": h.heading, "relevance_score": h.relevance_score}
                           for h in hits])

    return memory_search


def memory_researcher_subagent(tools: list[BaseTool], model: BaseChatModel) -> dict:
    return {
        "name": MEMORY_RESEARCHER,
        "description": "Searches the user's earlier reports for one Sub-task and replies "
                       "RESULT: or FAILED:.",
        "system_prompt": MEMORY_RESEARCHER_PROMPT,
        "tools": tools,
        "model": model,
    }
