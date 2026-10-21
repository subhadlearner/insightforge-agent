"""Spike fact: LangGraph interrupt/resume survives a new graph instance on a SQLite checkpoint."""

from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class S(TypedDict, total=False):
    answer: str


def _build(saver):
    def ask(state: S) -> S:
        return {"answer": interrupt("Which region?")}

    g = StateGraph(S)
    g.add_node("ask", ask)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g.compile(checkpointer=saver)


def test_interrupt_resumes_from_sqlite_checkpoint_in_new_graph_instance(tmp_path):
    db = str(tmp_path / "ck.db")
    cfg = {"configurable": {"thread_id": "t"}}
    with SqliteSaver.from_conn_string(db) as saver:
        first = _build(saver).invoke({}, cfg)
        assert first["__interrupt__"][0].value == "Which region?"
    # Simulates a process restart: new saver connection, new compiled graph.
    with SqliteSaver.from_conn_string(db) as saver:
        out = _build(saver).invoke(Command(resume="EMEA"), cfg)
    assert out["answer"] == "EMEA"
