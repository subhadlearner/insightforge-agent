"""Regression tests for the second independent review of PR #77 (head 11a2357), one block per
finding. Each is adversarial: the scripted model gives an incomplete, malformed or partial
answer that the code at 11a2357 recorded as complete or as a negative judgment. Scripted models
only: these prove code paths, not how often a live model errs."""

import json

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import (
    EvidenceItem,
    ExtractionResult,
    PassageRef,
    ResearchResults,
)
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.requirements import (
    Requirement,
    RequirementLedger,
    assess,
    gap_lines,
)
from tests.pipeline.test_extraction_synthesis import world  # noqa: F401
from tests.scripted import COVERAGE_PREFIX, ScriptedChat, coverage_verdict, verify_verdicts

PLAN = SubTaskPlan(aspects=[Aspect(id="a1", name="Cloud revenue")], sub_tasks=[
    SubTask(id="s1", aspect_id="a1", query="cloud revenue 2025", source_type="web",
            time_horizon_months=12, priority=1)])
THREE = RequirementLedger(origin="brief", requirements=[Requirement(
    id="R1", question="What was the 2025 cloud revenue of AWS, Azure and Google Cloud?",
    aspect_ids=["a1"])])
REF = PassageRef(observation_id="o", index=0)


def item(id_: str, statement: str) -> EvidenceItem:
    return EvidenceItem(id=id_, subtask_id="s1", aspect_id="a1", confidence="MEDIUM",
                        statement=statement, supporting=[REF], primary=REF,
                        requirement_ids=["R1"])


AWS = item("ev_aws", "AWS reported cloud revenue of $107.6 billion in 2025.")
OTHERS = item("ev_rest", "Azure revenue was $91 billion and Google Cloud revenue was $58 billion "
                         "in 2025.")


def answering(reply) -> ScriptedChat:
    """A light model that answers the requirement check with `reply(user)` (a dict)."""
    def chat(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        assert system.startswith("You check which numbered evidence statements answer")
        return AIMessage(content=json.dumps(reply(str(messages[-1].content))))
    return ScriptedChat(reply=chat)


def honest(user: str) -> dict:
    """Splits the question per company and says which statement states which company."""
    parts = ["AWS", "Azure", "Google Cloud"]
    out = []
    for line in user.split("\n"):
        if line.startswith("[") and "] " in line:
            n, text = line[1:].split("] ", 1)
            stated = [k for k, p in enumerate(parts, start=1) if p in text]
            out.append({"statement": int(n), "parts": stated,
                        "excerpt": text.rstrip(".") if stated else ""})
    return {"parts": parts, "answers": out}


def status_of(deps, items):
    from insightforge_agent.pipeline.requirements import confirm_answers

    answers = confirm_answers(deps, "alice", "run_1", THREE, items)
    (status,) = assess(THREE, PLAN, ResearchResults(results=[]), ExtractionResult(), items,
                       answers=answers)
    return status, answers


# ==== P1: an incomplete answer does not satisfy a requirement ==============================

def test_aws_only_evidence_does_not_satisfy_a_three_company_requirement(make_deps):
    status, _ = status_of(make_deps(light_model=answering(honest)), [AWS])
    assert status.status == "partial" and status.evidence_ids == ["ev_aws"]
    (line,) = gap_lines([status])
    assert "answered only in part" in line and "Azure" in line and "Google Cloud" in line
    assert "AWS" not in line.split("did not establish", 1)[1]


def test_together_the_items_answer_every_part(make_deps):
    status, _ = status_of(make_deps(light_model=answering(honest)), [AWS, OTHERS])
    assert status.status == "supported" and gap_lines([status]) == []


# A lumped split is now overruled by the independent answer verifier, not a capitalisation
# rule (final PR #77 review): see test_research_integrity_review3.py, block 1.


def test_an_aspect_name_question_is_answered_without_repeating_its_words(make_deps):
    """The one-per-Aspect fallback asks "Sales": a statement need not repeat it to state the
    part (the verifier decides; no word of the question is required of the statement)."""
    from insightforge_agent.pipeline.requirements import confirm_answers

    ledger = RequirementLedger(origin="aspects", requirements=[
        Requirement(id="R1", question="Sales", aspect_ids=["a1"])])
    sold = item("ev_byd", "BYD sold 4.27 million vehicles in 2025.")

    def yes(user):
        return {"parts": ["Sales"], "answers": [
            {"statement": 1, "parts": [1], "excerpt": "BYD sold 4.27 million vehicles"}]}
    answers = confirm_answers(make_deps(light_model=answering(yes)), "alice", "run_1", ledger,
                              [sold])
    assert answers.answers == {"R1": ["ev_byd"]} and answers.failed == []


def test_a_part_the_question_does_not_ask_is_dropped_by_the_coverage_check(make_deps):
    """answers-4 (run_c11947913dc5): parts are the model's words, so whether a part is asked is
    the coverage check's judgment, not a words-of-the-question rule. An invented part goes, with
    the answer claimed for it; what it left out comes back as parts no statement states."""
    def chat(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        if system.startswith(COVERAGE_PREFIX):
            return AIMessage(content=json.dumps({"asks": [
                {"quote": "AWS", "covered_by": [1]}, {"quote": "Azure", "covered_by": []},
                {"quote": "Google Cloud", "covered_by": []}]}))
        if system.startswith("You verify claimed answers"):
            return AIMessage(content=json.dumps(verify_verdicts(str(messages[-1].content))))
        return AIMessage(content=json.dumps({"parts": ["AWS's cloud revenue", "Oracle Cloud"],
                                             "answers": [{"statement": 1, "parts": [1, 2],
                                                          "excerpt": "AWS reported cloud revenue"}]}))
    model = ScriptedChat(reply=chat, checks=False)
    status, answers = status_of(make_deps(light_model=model), [AWS])
    assert answers.failed == [] and answers.parts["R1"] == ["AWS's cloud revenue", "Azure",
                                                            "Google Cloud"]
    assert status.status == "partial" and status.missing_parts == ["Azure", "Google Cloud"]


# ==== P1: a missing, duplicate or conflicting decision is a failed check ==================

def test_missing_duplicate_or_conflicting_decisions_are_verification_failures(make_deps):
    entry = {"statement": 1, "parts": [1], "excerpt": "AWS reported cloud revenue"}
    for name, answers in (
            ("duplicate", [entry, entry]),
            ("conflicting", [entry, {"statement": 1, "parts": [], "excerpt": ""}]),
            ("unknown statement", [entry, {"statement": 7, "parts": [], "excerpt": ""}]),
            ("unknown part", [{"statement": 1, "parts": [9], "excerpt": "AWS"}]),
            ("fabricated quote", [{"statement": 1, "parts": [1],
                                   "excerpt": "AWS revenue grew 40%"}])):
        model = answering(lambda user, a=answers: {"parts": ["AWS", "Azure", "Google Cloud"],
                                                   "answers": a})
        status, result = status_of(make_deps(light_model=model), [AWS])
        assert result.failed == ["R1"], name
        assert status.status == "verification_incomplete", name


def test_a_statement_left_out_states_no_part(make_deps):
    """answers-4 (run_c11947913dc5): the proposer lists only the statements that state a part;
    a statement it leaves out is its judgment that the statement states none, never a failure."""
    def lists_none(user):
        return {"parts": ["AWS", "Azure", "Google Cloud"], "answers": []}
    status, result = status_of(make_deps(light_model=answering(lists_none)), [AWS])
    assert result.failed == [] and status.status == "no_evidence"


def test_an_explicit_no_is_a_negative_judgment_not_a_failure(make_deps):
    def says_no(user):
        return {"parts": ["AWS", "Azure", "Google Cloud"],
                "answers": [{"statement": 1, "parts": [], "excerpt": ""}]}
    status, result = status_of(make_deps(light_model=answering(says_no)), [AWS])
    assert result.failed == [] and status.status == "no_evidence"


# ==== P1: an unresolved Brief decomposition is disclosed as a gap ==========================
# Now decided by the semantic coverage check (final PR #77 review): see
# test_research_integrity_review3.py, block 1.


# ==== P1 + P2 end to end: the Report and the persisted Run summary =========================

def test_the_report_and_its_run_summary_carry_coverage_and_fact_check_gaps(make_deps):
    from insightforge_agent.domain.contracts import Brief
    from insightforge_agent.pipeline.graph import run_brief
    from tests.pipeline import fakes
    from tests.pipeline.test_happy_path import BRIEF
    from tests.scripted import support_verdicts

    base, writer = fakes.light(), fakes.writer()

    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        if system.startswith(COVERAGE_PREFIX) and str(messages[-1].content).startswith(
                f"Text: {BRIEF}"):  # the Brief's coverage: pricing is asked, not tracked
            return AIMessage(content=json.dumps(coverage_verdict(str(messages[-1].content),
                                                                 ("pricing",))))
        if writer.seen and system.startswith(
                "You check whether numbered passages support one statement"):
            return AIMessage(content=json.dumps({"verdicts": support_verdicts(
                str(messages[-1].content), lambda s, t: "INSUFFICIENT_EVIDENCE")}))
        return base._generate(messages).generations[0].message

    deps = make_deps(light_model=ScriptedChat(reply=reply, checks=False), writer_model=writer,
                     fact_check_rate=1.0)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    report = deps.repos.reports.get_for_run("alice", run.id)
    published = report.body["verified"]["draft"]["gaps"]
    # The fakes' requirement call fails, so the ledger is one per Aspect, and the coverage
    # check finds the Brief's "pricing" in no requirement: a gap line, not established.
    assert any('The Brief asks for "pricing"' in g for g in published)
    assert any("failed the independent fact-check" in g for g in published)
    summary = deps.repos.summaries.get_for_run("alice", run.id)
    assert any("failed the independent fact-check" in g for g in summary.gaps)
    assert any('The Brief asks for "pricing"' in g for g in summary.gaps)


# ==== P2 (assignment): over-budget Evidence is compressed before anything is cut ===========
# Compression is now a shorter wording judged equivalent both ways, never the structured fact
# (final PR #77 review): see test_research_integrity_review3.py, block 2.

def test_an_item_that_cannot_be_shortened_is_still_cut_whole():
    from insightforge_agent.domain.evidence import compress_to_budget

    plain = [item(f"ev_{n}", f"Plain statement number {n} with several words.") for n in range(3)]
    kept, cut, summarised = compress_to_budget(plain, lambda xs: len(xs) <= 2,
                                               shorten=lambda i: None)
    assert [i.id for i in cut] == ["ev_2"] and summarised == []
