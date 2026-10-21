"""ADR-0006 §3, §5: the requirements ledger through planning, Extraction and Synthesis, and
corroboration that is not fooled by syndicated copies. Scripted models only."""

import json

from langchain_core.messages import AIMessage

from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.requirements import Requirement, RequirementLedger
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.requirements import derive_requirements
from insightforge_agent.pipeline.synthesize import synthesize
from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline.test_extraction_synthesis import (  # noqa: F401
    BRIEF,
    NOW,
    OWNER,
    RUN,
    all_supported,
    light,
    world,
)
from tests.scripted import ScriptedChat

PLAN = SubTaskPlan(
    aspects=[Aspect(id="a1", name="Sales"), Aspect(id="a2", name="Pricing")],
    sub_tasks=[SubTask(id="s1", aspect_id="a1", query="q s1", source_type="web",
                       time_horizon_months=12, priority=1),
               SubTask(id="s2", aspect_id="a2", query="q s2", source_type="web",
                       time_horizon_months=12, priority=2)])
LEDGER = RequirementLedger(origin="brief", requirements=[
    Requirement(id="R1", question="BYD 2025 unit sales?", aspect_ids=["a1"]),
    Requirement(id="R2", question="Tesla 2025 unit sales?", aspect_ids=["a1"]),
    Requirement(id="R3", question="Price cuts?", aspect_ids=["a2"])])


def answering(content) -> ScriptedChat:
    return ScriptedChat(reply=lambda m: AIMessage(content=content))


# ---- derivation -------------------------------------------------------------------------------

def test_a_valid_reply_becomes_the_runs_ledger(make_deps):
    deps = make_deps(light_model=answering(json.dumps(
        {"requirements": [r.model_dump() for r in LEDGER.requirements]})))
    ledger = derive_requirements(deps, OWNER, RUN, "BYD and Tesla sales and pricing", PLAN)
    assert ledger.origin == "brief" and ledger.ids() == {"R1", "R2", "R3"}
    request = str(deps.light_model.seen[0][-1].content)
    assert "- a1: Sales" in request and "- a2: Pricing" in request
    (planned,) = [e.payload for e in deps.repos.events.read_after(OWNER, RUN)
                  if e.type == "requirements_planned"]
    assert planned["origin"] == "brief" and len(planned["requirements"]) == 3


def test_an_unusable_or_inconsistent_reply_falls_back_to_one_per_aspect(make_deps):
    for content in ("not json", json.dumps({"requirements": [
            {"id": "R1", "question": "Sales?", "aspect_ids": ["a1"]}]})):  # a2 uncovered
        deps = make_deps(light_model=answering(content))
        ledger = derive_requirements(deps, OWNER, RUN, "brief", PLAN)
        assert ledger.origin == "aspects" and [r.question for r in ledger.requirements] == [
            "Sales", "Pricing"]
        (fallback,) = [e.payload for e in deps.repos.events.read_after(OWNER, RUN)
                       if e.type == "requirements_fallback"]
        assert fallback["problems"]


# ---- Extraction: context and links ------------------------------------------------------------

def research_of(world, **found):
    return world.research(**found)


def test_extraction_is_told_the_subtasks_and_their_requirements_and_keeps_known_links(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.", title="EV Weekly")
    research = research_of(world, s1=["https://a.test/x"])
    model = light(extract_=lambda ps, u: [
        {"passage_index": n, "statement": t, "subtask": "s1", "requirements": ["R1", "R3", "R9"]}
        for n, t in ps], entail=all_supported)
    world.deps.light_model = model
    scope = research.passage_scope()
    result = extract(world.deps, OWNER, RUN, list(scope), scope, plan=PLAN, research=research,
                     ledger=LEDGER)
    (call,) = model.calls("You extract facts")
    assert "- s1: q s1" in call and "- R1: BYD 2025 unit sales?" in call
    assert "R3" not in call and "EV Weekly" not in call  # only s1's requirements; never a title
    (fact,) = result.facts
    assert (fact.subtask_id, fact.requirement_ids) == ("s1", ["R1"])  # R3, R9 not shown: dropped
    bundle = synthesize(world.deps, OWNER, RUN, BRIEF, PLAN, research, result, LEDGER)
    (item,) = [i for s in bundle.sections for i in s.items]
    assert item.requirement_ids == ["R1"]


def test_an_uploaded_document_gets_its_identity_line_and_a_web_page_does_not(world):
    obs = world.deps.store.ingest_document(
        owner_id=OWNER, run_id=RUN, content_hash="nw",
        filename="northwind-health-annual-report-2025.pdf",
        chunks=[PassageDraft(t, page=1) for t in (
            "Northwind Health revenue reached 612 million dollars in fiscal 2025.",
            "The pipeline holds 6 programmes in clinical trials.",
            "Northwind Health employed 2,140 people.")], fetched_at=NOW, credibility_score=0.7)
    world.sources["nw"] = obs.source_id
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.")
    model = light(extract_=lambda ps, u: [], entail=all_supported)
    world.deps.light_model = model
    research = research_of(world, s1=["nw"], s2=["https://a.test/x"])
    scope = research.passage_scope()
    extract(world.deps, OWNER, RUN, list(scope), scope, plan=PLAN, research=research)
    pdf, web = model.calls("You extract facts")
    assert "This is an uploaded document of Northwind Health (identity only, not a fact)." in pdf
    assert "uploaded document" not in web


# ---- Confidence: syndicated copies are one source ------------------------------------------

SYNDICATED = ("BYD sold 4.27 million vehicles in 2025, the carmaker said on Thursday in a "
              "statement that also confirmed its overseas expansion plans for Europe and Brazil.")


def test_the_same_story_on_two_domains_is_not_independent_corroboration(world):
    world.page("https://a.example.com/x", SYNDICATED, cred=0.5)
    world.page("https://b.other.org/y", SYNDICATED, cred=0.5)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.example.com/x", "https://b.other.org/y"])
    world.deps.light_model = light(draft=lambda ps, u: [
        {"statement": "BYD sold 4.27 million vehicles in 2025.", "passages": [n]} for n, _ in ps],
        entail=all_supported)
    (item,) = world.items(world.synth(plan, research))
    assert len(item.supporting) == 2 and item.confidence == "LOW"  # not HIGH
    (decided,) = world.events("confidence_decided")
    assert decided["independent_pair_found"] is False


def test_different_texts_on_two_domains_still_corroborate(world):
    world.page("https://a.example.com/x", "BYD sold 4.27 million vehicles in 2025.", cred=0.5)
    world.page("https://b.other.org/y", "BYD sold 4.27 million vehicles in 2025, data showed.",
               cred=0.5)
    plan = world.plan(("s1", "a1", 1))
    research = world.research(s1=["https://a.example.com/x", "https://b.other.org/y"])
    world.deps.light_model = light(draft=lambda ps, u: [
        {"statement": "BYD sold 4.27 million vehicles in 2025.", "passages": [n]} for n, _ in ps],
        entail=all_supported)
    (item,) = world.items(world.synth(plan, research))
    assert item.confidence == "HIGH"


# ---- the budget keeps every required answer ------------------------------------------------

def test_a_tight_budget_cuts_extra_items_before_the_only_answer_to_a_requirement(world):
    world.page("https://a.test/x", "\n\n".join(
        f"BYD sold {n}.5 million vehicles in 2025, a regional breakdown shows." for n in range(6))
        + "\n\nTesla sold 1.64 million vehicles in 2025.", cred=0.9)
    research = world.research(s1=["https://a.test/x"])
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Sales")], sub_tasks=[PLAN.sub_tasks[0]])
    ledger = RequirementLedger(origin="brief", requirements=LEDGER.requirements[:2])
    ledger.requirements[1].aspect_ids = ["a1"]
    world.deps.light_model = light(extract_=lambda ps, u: [
        {"passage_index": n, "statement": t, "requirements": ["R2"] if "Tesla" in t else ["R1"]}
        for n, t in ps], entail=all_supported)
    scope = research.passage_scope()
    extraction = extract(world.deps, OWNER, RUN, list(scope), scope, plan=plan,
                         research=research, ledger=ledger)
    world.deps.evidence_budget_tokens = 80  # room for two items of seven
    bundle = synthesize(world.deps, OWNER, RUN, BRIEF, plan, research, extraction, ledger)
    kept = [i.statement for s in bundle.sections for i in s.items]
    assert "Tesla sold 1.64 million vehicles in 2025." in kept and 2 <= len(kept) < 7
    cuts = world.events("evidence_cut")
    assert cuts and all(c["requirement_ids"] == ["R1"] and c["last_support_of"] == []
                        for c in cuts)  # only spare R1 items went; R1 keeps one


def test_when_only_last_supports_remain_the_cut_is_logged_as_such(world):
    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.\n\n"
               "Tesla sold 1.64 million vehicles in 2025.", cred=0.9)
    research = world.research(s1=["https://a.test/x"])
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Sales")], sub_tasks=[PLAN.sub_tasks[0]])
    ledger = RequirementLedger(origin="brief", requirements=[
        Requirement(id="R1", question="BYD?", aspect_ids=["a1"]),
        Requirement(id="R2", question="Tesla?", aspect_ids=["a1"])])
    world.deps.light_model = light(extract_=lambda ps, u: [
        {"passage_index": n, "statement": t, "requirements": ["R2"] if "Tesla" in t else ["R1"]}
        for n, t in ps], entail=all_supported)
    scope = research.passage_scope()
    extraction = extract(world.deps, OWNER, RUN, list(scope), scope, plan=plan,
                         research=research, ledger=ledger)
    world.deps.evidence_budget_tokens = 30  # not even both answers fit
    synthesize(world.deps, OWNER, RUN, BRIEF, plan, research, extraction, ledger)
    cuts = world.events("evidence_cut")
    assert cuts and all(c["last_support_of"] in (["R1"], ["R2"]) for c in cuts)  # never silent
