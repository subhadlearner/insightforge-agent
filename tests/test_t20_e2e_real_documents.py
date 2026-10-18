"""T20 B6: the scripted parts of the web E2E backend's real-document research
(`tests/web_e2e/real_documents.py`). The real Docling and fastembed models are exercised by the
browser test itself (web/e2e/real-pdf.spec.ts) and by tests/integration."""

from langchain_core.messages import HumanMessage

from tests.pipeline import fakes
from tests.web_e2e import real_documents as rd


def test_only_the_pdf_brief_itself_gets_a_document_plan():
    plan = rd.planner([HumanMessage(f"PLAN. Allowed source types: documents. Brief: {rd.PDF_BRIEF_MARKER} annual report")])
    (submitted,) = plan.tool_calls
    assert submitted["name"] == "submit_plan"
    assert {t["source_type"] for t in submitted["args"]["plan"]["sub_tasks"]} == {"documents"}
    # A web Brief whose earlier Run summaries mention the PDF Brief plans as before.
    other = rd.planner([HumanMessage(
        f"PLAN. Allowed source types: web. Brief: {fakes.QUERIES['s1']}\nEarlier Runs (summaries, "
        f"newest first; context for planning only):\n- {rd.PDF_BRIEF_MARKER} annual report")])
    assert {t["source_type"] for t in other.tool_calls[0]["args"]["plan"]["sub_tasks"]} == {"web"}


def test_research_dispatches_exactly_the_listed_document_subtasks():
    listing = "\n".join(f"- {sid}: {q} [subagent_type doc_reader]" for sid, q in rd.DOC_QUERIES.items())
    reply = rd.planner([HumanMessage(f"RESEARCH: dispatch exactly these approved Sub-tasks now:\n{listing}\nrule")])
    assert [(c["args"]["description"], c["args"]["subagent_type"]) for c in reply.tool_calls] == [
        (f"SUBTASK_ID={sid} {q}", "doc_reader") for sid, q in rd.DOC_QUERIES.items()]


def test_without_the_models_nothing_is_wired_and_the_reason_is_given(monkeypatch):
    overrides, capabilities = rd.load("0")
    assert overrides == {} and capabilities == {"real_documents": False,
                                                "reason": "E2E_REAL_DOCUMENTS=0"}
