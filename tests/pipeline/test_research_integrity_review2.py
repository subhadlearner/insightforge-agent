"""Regression tests for the second independent review of PR #77 (head 11a2357), one block per
finding. Each is adversarial: the scripted model gives an incomplete, malformed or partial
answer that the code at 11a2357 recorded as complete or as a negative judgment. Scripted models
only: these prove code paths, not how often a live model errs."""

import json

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
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
from tests.scripted import ScriptedChat

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


def test_a_part_naming_three_companies_is_not_stated_by_one_of_them(make_deps):
    """A judge that keeps the question whole and calls AWS-only evidence an answer: the part
    names Azure and Google Cloud, which the statement does not, so code refuses it."""
    def lumping(user):
        return {"parts": ["AWS, Azure and Google Cloud"],
                "answers": [{"statement": 1, "parts": [1], "excerpt": "AWS reported cloud "
                             "revenue of $107.6 billion in 2025"}]}
    status, answers = status_of(make_deps(light_model=answering(lumping)), [AWS])
    assert status.status != "supported" and answers.answers == {"R1": []}


def test_an_aspect_name_question_holds_no_names_to_require(make_deps):
    """The one-per-Aspect fallback asks "Sales": its capitalised first word is not a name, so
    a statement need not repeat it to state the part."""
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


def test_parts_must_be_words_of_the_question(make_deps):
    def invented(user):
        return {"parts": ["Oracle Cloud"], "answers": [{"statement": 1, "parts": [], "excerpt": ""}]}
    status, answers = status_of(make_deps(light_model=answering(invented)), [AWS])
    assert answers.failed == ["R1"] and status.status == "verification_incomplete"


# ==== P1: a missing, duplicate or conflicting decision is a failed check ==================

def test_missing_duplicate_or_conflicting_decisions_are_verification_failures(make_deps):
    entry = {"statement": 1, "parts": [1], "excerpt": "AWS reported cloud revenue"}
    for name, answers in (
            ("missing", []),
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


def test_an_explicit_no_is_a_negative_judgment_not_a_failure(make_deps):
    def says_no(user):
        return {"parts": ["AWS", "Azure", "Google Cloud"],
                "answers": [{"statement": 1, "parts": [], "excerpt": ""}]}
    status, result = status_of(make_deps(light_model=answering(says_no)), [AWS])
    assert result.failed == [] and status.status == "no_evidence"


# ==== P1: an unresolved Brief decomposition is disclosed as a gap ==========================

def test_an_omitted_research_objective_is_disclosed_as_coverage_uncertainty(make_deps):
    from insightforge_agent.domain.requirements import decomposition_gaps
    from insightforge_agent.pipeline.requirements import derive_requirements

    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Cloud")], sub_tasks=[
        SubTask(id="s1", aspect_id="a1", query="q", source_type="web",
                time_horizon_months=12, priority=1)])
    omits = {"requirements": [{"id": "R1", "question": "AWS 2025 revenue growth?",
                               "aspect_ids": ["a1"]}]}
    deps = make_deps(light_model=ScriptedChat(
        reply=lambda m: AIMessage(content=json.dumps(omits))))
    ledger = derive_requirements(
        deps, "alice", "run_1",
        "Assess AWS revenue growth in 2025 and its capital spending on AI infrastructure.", plan)
    (line,) = decomposition_gaps(ledger)
    assert "may not cover all of the Brief" in line
    assert all(t in line for t in ('"capital"', '"spending"', '"infrastructure"'))
    assert "not established" not in line  # uncertainty, not a claim that a question is missing
    complete = ledger.model_copy(update={"uncovered_brief_terms": []})
    assert decomposition_gaps(complete) == []


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
        if writer.seen and system.startswith(
                "You check whether numbered passages support one statement"):
            return AIMessage(content=json.dumps({"verdicts": support_verdicts(
                str(messages[-1].content), lambda s, t: "INSUFFICIENT_EVIDENCE")}))
        return base._generate(messages).generations[0].message

    deps = make_deps(light_model=ScriptedChat(reply=reply), writer_model=writer,
                     fact_check_rate=1.0)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    report = deps.repos.reports.get_for_run("alice", run.id)
    published = report.body["verified"]["draft"]["gaps"]
    # The fakes' requirement call fails, so the ledger is one per Aspect: Brief words such as
    # the company names are in no requirement, and the Report says its coverage is uncertain.
    assert any("may not cover all of the Brief" in g for g in published)
    assert any("failed the independent fact-check" in g for g in published)
    summary = deps.repos.summaries.get_for_run("alice", run.id)
    assert any("failed the independent fact-check" in g for g in summary.gaps)
    assert any("may not cover all of the Brief" in g for g in summary.gaps)


# ==== P2 (assignment): over-budget Evidence is summarised before anything is cut ===========

def test_lower_ranked_evidence_is_summarised_to_its_fact_before_any_is_cut(world):
    from insightforge_agent.pipeline.extract import extract
    from insightforge_agent.pipeline.synthesize import bundle_tokens, synthesize
    from insightforge_agent.pipeline.write import render_bundle
    from tests.pipeline.test_extraction_synthesis import BRIEF, OWNER, RUN, light

    long = ("BYD sold {n}.5 million vehicles in region {n} in 2025, the carmaker said on "
            "Thursday in a statement that also described its dealer network and its plans.")
    world.page("https://a.test/x", "\n\n".join(long.format(n=n) for n in range(4)), cred=0.9)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.test/x"])
    world.deps.light_model = light(extract_=lambda ps, u: [
        {"passage_index": n, "statement": t, "entity": "BYD", "predicate": "units_sold",
         "value": t.split(" sold ")[1].split(" vehicles")[0], "scope": t.split(" in ")[1].split(
             " in 2025")[0], "period": "2025"} for n, t in ps], entail=lambda ps, u: [
        {"passage": n, "verdict": "SUPPORTED"} for n, _ in ps])
    scope = research.passage_scope()
    extraction = extract(world.deps, OWNER, RUN, list(scope), scope, plan=plan,
                         research=research)
    full = synthesize(world.deps, OWNER, RUN, BRIEF, plan, research, extraction)
    world.deps.evidence_budget_tokens = bundle_tokens(full) - 40  # over by a little
    bundle = synthesize(world.deps, OWNER, RUN, BRIEF, plan, research, extraction)
    items = [i for s in bundle.sections for i in s.items]
    assert len(items) == 4  # nothing cut: summarised instead
    summarised = [i for i in items if i.compressed]
    assert summarised and bundle_tokens(bundle) <= world.deps.evidence_budget_tokens
    shown = render_bundle(bundle)
    for i in summarised:
        assert i.statement not in shown and f"[{i.id}]" in shown  # same id, same citation
        assert i.primary == next(j for j in [x for s in full.sections for x in s.items]
                                 if j.id == i.id).primary
    compressed = world.events("evidence_compressed")
    assert {e["evidence_id"] for e in compressed} >= {i.id for i in summarised}
    assert all(e["reason"].startswith("over evidence budget") for e in compressed)


def test_an_item_with_no_structured_fact_is_still_cut_whole_and_logged():
    from insightforge_agent.domain.evidence import compress_to_budget

    plain = [item(f"ev_{n}", f"Plain statement number {n} with several words.") for n in range(3)]
    kept, cut, summarised = compress_to_budget(plain, lambda xs: len(xs) <= 2,
                                               shorten=lambda i: None)
    assert [i.id for i in cut] == ["ev_2"] and summarised == []


def test_the_bundle_renders_a_summarised_item_with_its_id_and_period():
    from insightforge_agent.pipeline.write import render_bundle

    fact = AWS.model_copy(update={"entity": "AWS", "predicate": "cloud_revenue",
                                  "value": "$107.6 billion", "as_of_period": "2025",
                                  "compressed": True})
    shown = render_bundle(EvidenceBundle(sections=[EvidenceSection(
        aspect_id="a1", heading="Cloud", confidence="MEDIUM", items=[fact])]))
    assert "[ev_aws] (MEDIUM; as of 2025; summarised) AWS: cloud revenue $107.6 billion" in shown
