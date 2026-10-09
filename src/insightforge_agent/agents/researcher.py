"""The Web Search Researcher: its tool, prompt and subagent definition.

The tool does the fetching. It stores each page through `SourceStore`, has the cheaper model
write a one-line summary in its own bounded call, and hands the Researcher only
`{source_id, title, summary}`. The Researcher model never sees a Source body."""

import json
import re
import threading
from collections.abc import Callable
from datetime import datetime

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, tool

from insightforge_agent.agents.web import (
    FetchError,
    PageFetcher,
    PaywalledError,
    SearchProvider,
    credibility_score,
)
from insightforge_agent.domain.contracts import SourceRef
from insightforge_agent.domain.models import RunEvent
from insightforge_agent.domain.passages import split_paragraphs
from insightforge_agent.domain.repositories import RunEventRepository
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.stores.source_store import SourceStore

SOURCE_ID = re.compile(r"src_[0-9a-f]{16}")
RESULTS_PER_SEARCH = 5
SUMMARY_MAX_CHARS = 300
SUMMARY_EVENT = "source_summarized"  # durable record of {source_id, title, summary}

RESEARCHER_PROMPT = """You are a Web Search Researcher. Your task description starts with
"SUBTASK_ID=<id>" followed by your research query. Call `web_search` with that query
(once more with a refined query only if the first returns nothing useful). It returns
sources as {source_id, title, summary}. Then reply with exactly one line:
"RESULT: <one sentence on what you found> | sources: <source_id>, <source_id>, ..."
listing only source_ids that `web_search` returned. If nothing useful was found, reply
"FAILED: <reason>"."""

SUMMARY_PROMPT = (
    "Write a single plain sentence (at most 40 words) saying what this web page says that "
    "is relevant to the research query. Reply with the sentence only."
)


def source_ids_in(result_text: str) -> list[str]:
    """Source IDs a Researcher named in its reply, in order, without repeats."""
    return list(dict.fromkeys(SOURCE_ID.findall(result_text)))


def _lead_passages(body: str, token_cap: int) -> str:
    """The page's leading paragraphs that fit the cap: a one-line summary is built from the
    lead of the page, and the full text stays available as Passages in the store."""
    taken, used = [], 0
    for p in split_paragraphs(body):
        cost = estimate_tokens(p)
        if taken and used + cost > token_cap:
            break
        taken.append(p)
        used += cost
    return "\n\n".join(taken)


def make_web_search_tool(
    *,
    owner_id: str,
    run_id: str,
    search: SearchProvider,
    fetcher: PageFetcher,
    store: SourceStore,
    summarizer: BaseChatModel,
    events: RunEventRepository,
    now: Callable[[], datetime],
    passage_token_cap: int,
) -> BaseTool:
    store_lock = threading.Lock()  # one SQLite connection is shared by parallel Researchers

    def log(type_: str, **payload) -> None:
        with store_lock:
            events.append(RunEvent(owner_id=owner_id, run_id=run_id, type=type_,
                                   payload=payload, created_at=now()))

    @tool
    def web_search(query: str) -> str:
        """Search the web for the query, fetch the top results and return their
        {source_id, title, summary} as JSON."""
        refs: list[SourceRef] = []
        for hit in search.search(query, RESULTS_PER_SEARCH):
            try:
                page = fetcher.fetch(hit.url)
            except PaywalledError as e:
                log("source_skipped", url=hit.url, reason="paywalled", detail=str(e))
                continue
            except FetchError as e:
                log("source_skipped", url=hit.url, reason="fetch_failed", detail=str(e))
                continue
            fetched_at = now()
            with store_lock:
                obs = store.ingest_web(
                    owner_id=owner_id, run_id=run_id, url=page.url, title=page.title,
                    body=page.body, fetched_at=fetched_at, published_at=page.published_at,
                    credibility_score=credibility_score(page.url, page.published_at, fetched_at),
                )
            lead = _lead_passages(page.body, passage_token_cap)
            if len(lead) < len(page.body):
                log("summary_input_limited", source_id=obs.source_id, chars_read=len(lead),
                    chars_total=len(page.body))
            reply = summarizer.invoke([
                SystemMessage(SUMMARY_PROMPT),
                HumanMessage(f"Query: {query}\nTitle: {page.title}\n\n"
                             f"{lead}"),
            ])
            line = " ".join(reply.text.split())[:SUMMARY_MAX_CHARS]
            ref = SourceRef(source_id=obs.source_id, title=page.title, summary=line)
            log(SUMMARY_EVENT, **ref.model_dump())
            refs.append(ref)
        return json.dumps([r.model_dump() for r in refs])

    return web_search


def researcher_subagent(tools: list[BaseTool], model: BaseChatModel) -> dict:
    return {
        "name": "researcher",
        "description": "Researches one Sub-task on the web and replies RESULT: or FAILED:.",
        "system_prompt": RESEARCHER_PROMPT,
        "tools": tools,
        "model": model,
    }
