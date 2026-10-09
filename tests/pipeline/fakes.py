"""Scripted stand-ins for every model and for search, driven by recorded fixtures."""

import json
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from insightforge_agent.agents.web import Page, PaywalledError, SearchHit, FetchError
from tests.scripted import ScriptedChat, ai, call

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "search"

QUERIES = {
    "s1": "BYD battery electric vehicle sales 2025",
    "s2": "BYD Tesla pricing strategy 2025",
    "s3": "EV battery technology 2025 LFP solid state",
}
PLAN_ARGS = {"plan": {
    "aspects": [{"id": "a1", "name": "Sales"}, {"id": "a2", "name": "Pricing"},
                {"id": "a3", "name": "Batteries"}],
    "sub_tasks": [
        {"id": sid, "aspect_id": f"a{n}", "query": q, "source_type": "web",
         "time_horizon_months": 12, "priority": n}
        for n, (sid, q) in enumerate(QUERIES.items(), start=1)
    ],
}}


class FixtureSearch:
    def __init__(self, name: str = "ev_market.json") -> None:
        self._data = json.loads((FIXTURES / name).read_text())
        self.queries: list[str] = []

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.queries.append(query)
        return [SearchHit(h["url"], h["title"]) for h in self._data["queries"][query]][:limit]

    def fetch(self, url: str) -> Page:
        page = self._data["pages"][url]
        if page.get("paywalled"):
            raise PaywalledError(url)
        if page.get("error"):
            raise FetchError(url)
        return Page(url=url, title=page["title"], body=page["body"])


def task(sid: str, call_id: str) -> dict:
    return call("task", {"description": f"SUBTASK_ID={sid} {QUERIES[sid]}",
                         "subagent_type": "researcher"}, call_id)


def planner(research_ids=("s1", "s2", "s3"), repair_ids=None) -> ScriptedChat:
    """`repair_ids=None` scripts no repair turn; `()` scripts one that dispatches nothing."""
    todos = call("write_todos", {"todos": [
        {"content": q, "status": "pending"} for q in QUERIES.values()]}, "todo-1")
    script = [
        ai(call("submit_plan", PLAN_ARGS, "plan-1")),
        ai(text="Plan submitted."),
        ai(todos, *[task(s, f"research-{s}") for s in research_ids]),
        ai(text="Dispatched."),
    ]
    if repair_ids is not None:
        script += [ai(*[task(s, f"repair-{s}") for s in repair_ids]), ai(text="Repair done.")]
    return ScriptedChat(script=script)


def researcher() -> ScriptedChat:
    """Calls web_search with its task's query, then names the sources it was given."""
    def reply(messages) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            ids = [r["source_id"] for r in json.loads(str(last.content))]
            return AIMessage(content=f"RESULT: found material | sources: {', '.join(ids)}")
        text = "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
        query = text.split(" ", 1)[1] if "SUBTASK_ID=" in text else text
        if "FAIL-ME" in text:
            return AIMessage(content="FAILED: nothing relevant")
        return ai(call("web_search", {"query": query.strip()}, f"search-{abs(hash(text))}"))
    return ScriptedChat(reply=reply)


def light() -> ScriptedChat:
    """Summaries and extraction, told apart by the system prompt."""
    def reply(messages) -> AIMessage:
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        if system.startswith("You extract facts"):
            facts = [
                {"passage_index": int(i), "statement": text.strip(), "entities": ["BYD"]}
                for i, text in re.findall(r"\[(\d+)\] (.+)", user)
            ]
            return AIMessage(content=json.dumps({"facts": facts}))
        return AIMessage(content="A page about electric vehicles.")
    return ScriptedChat(reply=reply)


def writer(bad_first: int = 0) -> ScriptedChat:
    """Cites every evidence item. The first `bad_first` replies cite an unknown id."""
    state = {"n": 0}

    def reply(messages) -> AIMessage:
        user = str(messages[-1].content)
        items = re.findall(r"- \[(ev_\w+)\] \(\w+\) (.+)", user)
        state["n"] += 1
        bad = state["n"] <= bad_first
        claims = [{"text": t, "evidence_id": "ev_nope" if bad else eid} for eid, t in items]
        return AIMessage(content=json.dumps({
            "title": "EV report", "summary": "Summary.",
            "sections": [{"heading": "Findings", "claims": claims}]}))
    return ScriptedChat(reply=reply)
