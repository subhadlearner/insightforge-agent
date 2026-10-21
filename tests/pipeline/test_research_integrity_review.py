"""Regression tests for the independent review of PR #77 (head 86584c2), one block per finding.

Each test is adversarial: a scripted model gives the wrong, or a broken, answer that the
review showed the code at 86584c2 would publish or mis-record, and the test asserts the
pipeline refuses it or records it for what it is. Scripted models only: these prove code
paths, not how often a live model errs."""

import json
import re

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.agents.llm_json import BadModelReply
from insightforge_agent.domain.contracts import (
    Claim,
    ClaimVerdict,
    EvidenceItem,
    ExtractionResult,
    PassageRef,
    ReportDraft,
    ReportSection,
    ResearchResults,
)
from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import Window
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.requirements import (
    Requirement,
    RequirementLedger,
    RequirementStatus,
    assess,
    gap_lines,
)
from insightforge_agent.domain.support import SupportPart, check_joint, check_support
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.write import write_report
from tests.pipeline.test_extraction_synthesis import world  # noqa: F401
from tests.pipeline.test_t6_writer_fact_check import URL, Fetcher, evidence
from tests.scripted import ScriptedChat, support_verdicts

JOINT = "You check whether numbered passages, taken together"
SUPPORT = "You check whether numbered passages support one statement"


def contains(statement: str, text: str) -> bool:
    return statement.rstrip(".").casefold() in text.casefold()


def judge(verdict=lambda statement, text: "SUPPORTED" if contains(statement, text)
          else "INSUFFICIENT_EVIDENCE", joint=None, answers=None) -> ScriptedChat:
    """A scripted light model for every check that reads text. `verdict(statement, text)`
    answers the support judgment per unit (SUPPORTED with the permissive whole-text map);
    `joint(statement, texts)` the joint one (default: SUPPORTED when `verdict` supports the
    statement in the units' joined text); `answers(question, statement)` the requirement check
    (default: every statement answers)."""
    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        statement = user.split("\n", 1)[0].removeprefix("Statement: ")
        units = re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE)
        if system.startswith(JOINT):
            texts = [t for _, t in units]
            v = (joint or (lambda s, ts: verdict(s, " ".join(ts))))(statement, texts)
            parts = ([{"part": statement, "excerpts": [{"passage": int(n), "text": t}
                                                      for n, t in units]}]
                     if v == "SUPPORTED" else [])
            return AIMessage(content=json.dumps({"verdict": v, "parts": parts}))
        if system.startswith(SUPPORT):
            return AIMessage(content=json.dumps(
                {"verdicts": support_verdicts(user, verdict)}))
        if system.startswith("You check which numbered evidence statements answer"):
            question = user.split("\n", 1)[0].removeprefix("Question: ")
            out = []
            for n, t in units:
                yes = (answers or (lambda q, s: True))(question, t)
                out.append({"statement": int(n), "answers": yes, "excerpt": t if yes else ""})
            return AIMessage(content=json.dumps({"answers": out}))
        return AIMessage(content="A page.")
    return ScriptedChat(reply=reply)


def events(deps, type_):
    return [e.payload for e in deps.repos.events.read_after("alice", "run_1") if e.type == type_]


def claims_of(draft):
    return [c for s in draft.sections for c in s.claims]


# ==== P1: the Fact-Checker verifies the citation that is displayed ===========================

CLAIM = "BYD sold 4.27 million vehicles in 2025."
WRONG = "In 2025 BYD said it could one day sell 4.27 million vehicles a year."


def test_support_elsewhere_on_the_page_never_verifies_a_citation_that_does_not_support(
        make_deps):
    """The Claim cites Passage 0 (WRONG); Passage 1 of the same page states it. At 86584c2 the
    Passage-1 SUPPORTED made the Claim verified while the drawer showed Passage 0."""
    deps = make_deps(light_model=judge(), fetcher=Fetcher({URL: f"{WRONG}\n\n{CLAIM}"}))
    _, bundle = evidence(deps, [WRONG, CLAIM])
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=CLAIM, evidence_id="ev_0")])])
    (v,) = fact_check(deps, "alice", "run_1", draft, bundle).verdicts
    assert v.verdict == "unverified"
    assert v.cited_support == "INSUFFICIENT_EVIDENCE"
    assert "not by the cited Passage" in v.reason
    assert v.matched is not None and v.matched.index == 1  # where support was found, recorded


def test_the_same_holds_for_an_uploaded_document(make_deps):
    deps = make_deps(light_model=judge())
    _, bundle = evidence(deps, [WRONG, CLAIM], kind="pdf")
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=CLAIM, evidence_id="ev_0")])])
    (v,) = fact_check(deps, "alice", "run_1", draft, bundle).verdicts
    assert v.verdict == "unverified" and "not by the cited Passage" in v.reason


def test_a_citation_that_supports_the_claim_is_still_verified(make_deps):
    deps = make_deps(light_model=judge(), fetcher=Fetcher({URL: f"{WRONG}\n\n{CLAIM}"}))
    _, bundle = evidence(deps, [WRONG, CLAIM])
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=CLAIM, evidence_id="ev_1")])])
    (v,) = fact_check(deps, "alice", "run_1", draft, bundle).verdicts
    assert (v.verdict, v.cited_support) == ("verified", "SUPPORTED")


# ==== P1: every reworded Claim is checked before publication ================================

DECLINED = "Northwind Health revenue declined 20% in 2025."
INCREASED = "Northwind Health revenue increased 20% in 2025."
EXPECTED = "NW-200 approval is expected in 2026."
GRANTED = "NW-200 approval was granted in 2026."


def one_shot_writer(claims, summary) -> ScriptedChat:
    return ScriptedChat(reply=lambda m: AIMessage(content=json.dumps(
        {"title": "T", "summary": summary,
         "sections": [{"heading": "Sales", "claims": claims}]})))


def test_a_claim_that_reverses_its_evidence_is_never_published(make_deps):
    """Same names, same numbers: only the meaning changed, which no lexical rule sees."""
    deps = make_deps(light_model=judge(),
                     writer_model=one_shot_writer(
                         [{"text": INCREASED, "evidence_id": "ev_0"},
                          {"text": GRANTED, "evidence_id": "ev_1"}], DECLINED),
                     fetcher=Fetcher({}))
    _, bundle = evidence(deps, [DECLINED, EXPECTED])
    draft = write_report(deps, "alice", "run_1", "Northwind", bundle, [])
    texts = [c.text for c in claims_of(draft)]
    assert INCREASED not in texts and GRANTED not in texts
    assert texts == [DECLINED, EXPECTED]  # the verified statements, verbatim, and said so
    (rejected,) = events(deps, "writer_claims_checked")
    assert {r["evidence_id"] for r in rejected["rejected"]} == {"ev_0", "ev_1"}
    assert "claims_restated_verbatim:2" in draft.quality


def test_a_faithful_rewording_is_judged_and_kept(make_deps):
    reworded = "In 2025, Northwind Health revenue declined 20%."
    deps = make_deps(light_model=judge(verdict=lambda s, t: "SUPPORTED"),
                     writer_model=one_shot_writer([{"text": reworded, "evidence_id": "ev_0"}],
                                                  DECLINED))
    _, bundle = evidence(deps, [DECLINED])
    draft = write_report(deps, "alice", "run_1", "Northwind", bundle, [])
    assert [c.text for c in claims_of(draft)] == [reworded] and draft.quality == []
    (checked,) = events(deps, "writer_claims_checked")
    assert checked["judged"] == 1 and checked["rejected"] == []


def test_the_runs_token_cap_stops_the_run_instead_of_skipping_the_check(make_deps):
    import pytest

    from insightforge_agent.pipeline.errors import RunFailed

    deps = make_deps(light_model=judge(), run_token_cap=10,
                     writer_model=one_shot_writer([{"text": INCREASED, "evidence_id": "ev_0"}],
                                                  DECLINED))
    _, bundle = evidence(deps, [DECLINED])
    with pytest.raises(RunFailed, match="token cap"):
        write_report(deps, "alice", "run_1", "Northwind", bundle, [])


def test_an_exact_restatement_needs_no_second_judgment(make_deps):
    deps = make_deps(light_model=judge(),
                     writer_model=one_shot_writer([{"text": DECLINED, "evidence_id": "ev_0"}],
                                                  DECLINED))
    _, bundle = evidence(deps, [DECLINED])
    write_report(deps, "alice", "run_1", "Northwind", bundle, [])
    assert events(deps, "support_judged") == []  # verified once, in Synthesis


# ==== P1: a requirement is answered only by Evidence that answers it ========================

PLAN = SubTaskPlan(aspects=[Aspect(id="a1", name="AMD in China")], sub_tasks=[
    SubTask(id="s1", aspect_id="a1", query="AMD China", source_type="web",
            time_horizon_months=12, priority=1)])
AMD_LEDGER = RequirementLedger(origin="brief", requirements=[
    Requirement(id="R1", question="What was AMD's China revenue in 2025?", aspect_ids=["a1"])])
LAUNCH = EvidenceItem(
    id="ev_x", subtask_id="s1", aspect_id="a1", confidence="MEDIUM",
    statement="AMD launched the MI308 accelerator for customers in China.",
    supporting=[PassageRef(observation_id="o", index=0)],
    primary=PassageRef(observation_id="o", index=0), requirement_ids=["R1"])


def test_a_valid_but_wrong_requirement_id_does_not_answer_the_requirement(make_deps):
    from insightforge_agent.pipeline.requirements import confirm_answers

    deps = make_deps(light_model=judge(answers=lambda q, s: "revenue" in s))
    answers = confirm_answers(deps, "alice", "run_1", AMD_LEDGER, [LAUNCH])
    (status,) = assess(AMD_LEDGER, PLAN, ResearchResults(results=[]), ExtractionResult(),
                       [LAUNCH], answers=answers)
    assert status.status == "no_evidence" and status.evidence_ids == []
    assert any("did not answer it" in r for r in status.reasons)
    (line,) = gap_lines([status])
    assert line.startswith("Requirement R1 (What was AMD's China revenue in 2025?)")


def test_a_failed_requirement_check_is_verification_incomplete_not_answered(make_deps):
    from insightforge_agent.pipeline.requirements import confirm_answers

    deps = make_deps(light_model=ScriptedChat(reply=lambda m: AIMessage(content="no json")))
    answers = confirm_answers(deps, "alice", "run_1", AMD_LEDGER, [LAUNCH])
    (status,) = assess(AMD_LEDGER, PLAN, ResearchResults(results=[]), ExtractionResult(),
                       [LAUNCH], answers=answers)
    assert status.status == "verification_incomplete" and status.evidence_ids == []


def test_an_answer_quote_that_is_not_in_the_statement_does_not_count(make_deps):
    from insightforge_agent.pipeline.requirements import confirm_answers

    lying = ScriptedChat(reply=lambda m: AIMessage(content=json.dumps({"answers": [
        {"statement": 1, "answers": True, "excerpt": "AMD's China revenue was $6.2 billion"}]})))
    deps = make_deps(light_model=lying)
    answers = confirm_answers(deps, "alice", "run_1", AMD_LEDGER, [LAUNCH])
    assert answers.answers == {"R1": []}


def test_brief_terms_no_requirement_covers_are_sent_back_once_then_recorded(make_deps):
    from insightforge_agent.pipeline.requirements import derive_requirements

    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Cloud")], sub_tasks=[
        SubTask(id="s1", aspect_id="a1", query="q", source_type="web",
                time_horizon_months=12, priority=1)])
    first = {"requirements": [{"id": "R1", "question": "AWS 2025 revenue growth?",
                               "aspect_ids": ["a1"]}]}
    second = {"requirements": first["requirements"] + [
        {"id": "R2", "question": "AWS capital spending on AI infrastructure?",
         "aspect_ids": ["a1"]}]}
    replies = iter([first, second])
    deps = make_deps(light_model=ScriptedChat(
        reply=lambda m: AIMessage(content=json.dumps(next(replies)))))
    brief = "Assess AWS revenue growth in 2025 and its capital spending on AI infrastructure."
    ledger = derive_requirements(deps, "alice", "run_1", brief, plan)
    assert ledger.ids() == {"R1", "R2"} and ledger.uncovered_brief_terms == []
    retry = str(deps.light_model.seen[1][-1].content)
    assert "capital" in retry and "spending" in retry

    stubborn = ScriptedChat(reply=lambda m: AIMessage(content=json.dumps(first)))
    deps = make_deps(light_model=stubborn)
    ledger = derive_requirements(deps, "alice", "run_1", brief, plan)
    assert len(stubborn.seen) == 2  # one retry, no more
    assert {"capital", "spending"} <= set(ledger.uncovered_brief_terms)


# ==== P1: logical operators are not disposable ===============================================

def test_an_omitted_negation_in_the_statement_is_refused():
    statement = "Revenue did not increase by 20% in 2025."
    passage = "Revenue increased by 20% in 2025."
    check = check_support(statement, [SupportPart(statement.rstrip("."), (passage,))],
                          passage, None)
    assert check.verdict == "INSUFFICIENT_EVIDENCE"
    assert any("not" in p for p in check.problems)


def test_a_negation_trimmed_off_the_quote_is_refused():
    statement = "Revenue increased by 20% in 2025."
    passage = "Revenue did not increase by 20% in 2025, the company said."
    parts = [SupportPart("Revenue", ("Revenue",)),
             SupportPart("increased by 20% in 2025", ("increase by 20% in 2025",))]
    assert check_support(statement, parts, passage, None).verdict == "INSUFFICIENT_EVIDENCE"


def test_a_hedge_trimmed_off_the_quote_is_refused():
    statement = "NW-200 will receive approval in 2026."
    passage = "Northwind said NW-200 is expected to receive approval in 2026."
    parts = [SupportPart("NW-200", ("NW-200",)),
             SupportPart("will receive approval in 2026", ("receive approval in 2026",))]
    check = check_support(statement, parts, passage, None)
    assert check.verdict == "SUPPORTED"  # a hedge both sides keep: "will" and "expected"
    flat = "NW-200 received approval in 2026."
    parts = [SupportPart("NW-200", ("NW-200",)),
             SupportPart("received approval in 2026", ("receive approval in 2026",))]
    assert check_support(flat, parts, passage, None).verdict == "INSUFFICIENT_EVIDENCE"


def test_only_and_all_must_be_stated_by_the_quotes():
    passage = "AWS is a provider growing 20% a year, as are Azure and Google Cloud."
    for statement in ("AWS is the only provider growing 20% a year.",
                      "All providers are growing 20% a year."):
        check = check_support(statement, [SupportPart(statement.rstrip("."), (passage,))],
                              passage, None)
        assert check.verdict == "INSUFFICIENT_EVIDENCE", statement


def test_the_operators_are_held_in_a_joint_judgment_too_but_a_comparison_may_be_drawn():
    texts = ["BYD sold 4.27 million vehicles in 2025.", "Tesla sold 1.64 million vehicles in 2025."]
    from insightforge_agent.domain.support import JointPart

    drawn = "BYD sold 4.27 million vehicles in 2025, more than Tesla's 1.64 million."
    ok = check_joint(drawn, [JointPart(drawn.rstrip("."), ((1, texts[0]), (2, texts[1])))],
                     texts, [None, None])
    assert ok.verdict == "SUPPORTED"  # the comparison is the analysis: no source states it
    negated = "BYD did not sell more vehicles in 2025 than Tesla's 1.64 million."
    bad = check_joint(negated, [JointPart(negated.rstrip("."), ((1, texts[0]), (2, texts[1])))],
                      texts, [None, None])
    assert bad.verdict == "INSUFFICIENT_EVIDENCE"


# ==== P1: a failed judgment is not "insufficient Evidence" =================================

def test_a_broken_support_reply_is_verification_incomplete_through_to_the_ledger(world):
    from insightforge_agent.domain.evidence import DROP_UNVERIFIED
    from insightforge_agent.domain.requirements import unverifiable
    from insightforge_agent.pipeline.extract import extract
    from insightforge_agent.pipeline.synthesize import synthesize
    from tests.pipeline.test_extraction_synthesis import BRIEF, OWNER, RUN, light
    from tests.pipeline.test_research_integrity_requirements import LEDGER, PLAN as SALES_PLAN

    world.page("https://a.test/x", "BYD sold 4.27 million vehicles in 2025.")
    research = world.research(s1=["https://a.test/x"])
    world.deps.light_model = light(
        extract_=lambda ps, u: [{"passage_index": n, "statement": t, "subtask": "s1",
                                 "requirements": ["R1"]} for n, t in ps],
        support=lambda ps, u: "truncated")  # every support reply unusable
    scope = research.passage_scope()
    extraction = extract(world.deps, OWNER, RUN, list(scope), scope, plan=SALES_PLAN,
                         research=research, ledger=LEDGER)
    bundle = synthesize(world.deps, OWNER, RUN, BRIEF, SALES_PLAN, research, extraction, LEDGER)
    assert bundle.sections == []
    (drop,) = world.events("evidence_dropped")
    assert drop["reason"] == DROP_UNVERIFIED and drop["requirement_ids"] == ["R1"]
    statuses = assess(LEDGER, SALES_PLAN, research, extraction, [],
                      unverifiable=unverifiable(world.events("evidence_dropped")))
    by_id = {s.id: s for s in statuses}
    assert by_id["R1"].status == "verification_incomplete"
    assert by_id["R2"].status == "no_evidence"  # nothing failed for R2: genuinely not found
    line = next(x for x in gap_lines(statuses) if x.startswith("Requirement R1"))
    assert "could not be checked" in line


# ==== P1: a rejected analytical conclusion cannot survive in the summary ====================

BYD = "BYD sold 4.27 million vehicles in 2025."
TESLA = "Tesla sold 1.64 million vehicles in 2025."
COMPARISON = "BYD sold 4.27 million vehicles in 2025, more than Tesla's 1.64 million."


def test_a_rejected_comparison_is_removed_from_the_summary_too(make_deps):
    def rejects_comparisons(statement, texts):
        return "INSUFFICIENT_EVIDENCE" if "more than" in statement else "SUPPORTED"

    writer = one_shot_writer([{"text": BYD, "evidence_id": "ev_0"},
                              {"text": TESLA, "evidence_id": "ev_1"},
                              {"text": COMPARISON, "evidence_id": "ev_0", "premises": ["ev_1"]}],
                             f"{COMPARISON} {TESLA}")
    deps = make_deps(light_model=judge(joint=rejects_comparisons), writer_model=writer,
                     fetcher=Fetcher({URL: f"{BYD}\n\n{TESLA}"}))
    _, bundle = evidence(deps, [BYD, TESLA])
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [])
    assert COMPARISON not in [c.text for c in claims_of(draft)]  # every section still covered
    assert "more than" not in draft.summary and TESLA in draft.summary
    (checked,) = events(deps, "summary_checked")
    assert checked["removed"] == [COMPARISON]
    assert "summary_sentences_removed:1" in draft.quality


def test_a_summary_sentence_the_published_claims_do_not_support_is_removed(make_deps):
    unsupported = "BYD outsold every rival in 2025."
    writer = one_shot_writer([{"text": BYD, "evidence_id": "ev_0"}], f"{unsupported} {BYD}")
    deps = make_deps(light_model=judge(joint=lambda s, ts: "INSUFFICIENT_EVIDENCE"),
                     writer_model=writer)
    _, bundle = evidence(deps, [BYD])
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [])
    assert draft.summary == BYD


def test_a_stated_limit_of_the_research_stays_in_the_summary_unjudged(make_deps):
    limit = "This research did not establish BYD's battery outlook."
    writer = one_shot_writer([{"text": BYD, "evidence_id": "ev_0"}], f"{BYD} {limit}")
    deps = make_deps(light_model=judge(joint=lambda s, ts: "INSUFFICIENT_EVIDENCE"),
                     writer_model=writer)
    _, bundle = evidence(deps, [BYD])
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [])
    assert draft.summary == f"{BYD} {limit}"
    (checked,) = events(deps, "summary_checked")
    assert (checked["judged"], checked["limitations"]) == (0, 1)


# ==== P2: the ledger reflects the Fact-Checker =============================================

def test_a_requirement_whose_claims_all_failed_fact_checking_is_not_left_reported():
    from insightforge_agent.domain.requirements import after_fact_check

    statuses = [
        RequirementStatus(id="R1", question="BYD sales?", priority="required", status="reported",
                          evidence_ids=["ev_0"], claims=["0.0"]),
        RequirementStatus(id="R2", question="Tesla sales?", priority="required",
                          status="reported", evidence_ids=["ev_1"], claims=["0.1", "0.2"]),
        RequirementStatus(id="R3", question="Prices?", priority="required", status="reported",
                          evidence_ids=["ev_2"], claims=["0.3"])]
    ref = PassageRef(observation_id="o", index=0)
    verdicts = [
        ClaimVerdict(section=0, claim=0, evidence_id="ev_0", original=ref, verdict="unverified",
                     check="web_refetch", cited_support="INSUFFICIENT_EVIDENCE"),
        ClaimVerdict(section=0, claim=1, evidence_id="ev_1", original=ref, verdict="unverified",
                     check="web_refetch", cited_support="SUPPORTED"),  # page drift
        ClaimVerdict(section=0, claim=3, evidence_id="ev_2", original=ref, verdict="unchecked",
                     check="web_refetch")]
    after = {s.id: s for s in after_fact_check(statuses, verdicts)}
    assert after["R1"].status == "reported_unverified"
    assert after["R1"].fact_check == {"0.0": "unverified"}
    assert after["R2"].status == "reported"  # drift is not a failed citation; 0.2 unsampled
    assert after["R2"].fact_check == {"0.1": "page_drift"}
    assert after["R3"].status == "reported" and after["R3"].fact_check == {"0.3": "unchecked"}
    (line,) = gap_lines(list(after.values()))
    assert line.startswith("Requirement R1 (BYD sales?)") and "fact-check" in line


def test_the_stored_ledger_and_gaps_show_a_failed_fact_check(make_deps):
    from insightforge_agent.domain.contracts import Brief
    from insightforge_agent.pipeline.graph import run_brief
    from tests.pipeline import fakes
    from tests.pipeline.test_happy_path import BRIEF as HAPPY

    base = fakes.light()
    writer = fakes.writer()

    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        if writer.seen and system.startswith(SUPPORT):  # the Fact-Checker, after writing
            user = str(messages[-1].content)
            return AIMessage(content=json.dumps({"verdicts": support_verdicts(
                user, lambda s, t: "INSUFFICIENT_EVIDENCE")}))
        return base._generate(messages).generations[0].message

    # Every Claim sampled, so the outcome does not depend on the Run id's seeded sample.
    deps = make_deps(light_model=ScriptedChat(reply=reply), writer_model=writer,
                     fact_check_rate=1.0)
    run = run_brief(deps, "alice", Brief(text=HAPPY))
    report = deps.repos.reports.get_for_run("alice", run.id)
    statuses = report.body["ledger"]["status"]
    assert statuses and all(s["claims"] for s in statuses)
    assert all(s["status"] == "reported_unverified" and set(s["fact_check"].values()) == {
        "unverified"} for s in statuses)
    gaps = report.body["verified"]["draft"]["gaps"]
    assert sum("failed the independent fact-check" in g for g in gaps) == len(statuses)
    assert any(q.startswith("requirements_unverified:") for q in
               report.body["verified"]["draft"]["quality"])


# ==== P2: one long Passage is not lost to one truncated reply ==============================

def cut_off() -> BadModelReply:
    from insightforge_agent.agents.llm_json import ReplyDiagnostic

    return BadModelReply(ReplyDiagnostic(
        schema="_Facts", kind="incomplete_json", error_count=1, error_types=("eof",),
        error="EOF", length=20, head='{"facts": [', tail="", stop_reason="max_tokens",
        output_tokens=4096, reasoning_tokens=None, max_tokens=4096), "truncated")


def test_a_single_long_passage_is_split_at_sentences_and_every_fact_kept(make_deps):
    from insightforge_agent.pipeline.work_units import run_unit

    deps = make_deps()
    text = " ".join(f"BYD sold {n}.5 million vehicles in region {n} in 2025." for n in range(4))
    passage = Passage(observation_id="o", index=0, text=text)
    seen = []

    def call(windows):
        shown = " ".join(w.text for w in windows)
        seen.append(shown)
        if shown.count("BYD sold") > 1:  # more than one sentence: cut at the output limit
            raise cut_off()
        return shown

    outcomes = run_unit(deps, "alice", "run_1", [Window(passage, text)], call,
                        stage="extraction", unit="u")
    assert [o.result for o in outcomes] == [f"BYD sold {n}.5 million vehicles in region {n} in "
                                            "2025." for n in range(4)]
    assert all(o.windows[0].passage is passage for o in outcomes)  # still cites the Passage
    assert len(seen) == 7  # 1 + 2 + 4: bounded by MAX_SPLIT_DEPTH, as for several Passages


def test_a_single_sentence_still_cannot_be_split(make_deps):
    from insightforge_agent.pipeline.work_units import run_unit

    deps = make_deps()
    passage = Passage(observation_id="o", index=0, text="BYD sold 4.27 million vehicles.")

    def call(windows):
        raise cut_off()

    (outcome,) = run_unit(deps, "alice", "run_1", [Window(passage, passage.text)], call,
                          stage="extraction", unit="u")
    assert outcome.result is None and outcome.reason == "truncated"
