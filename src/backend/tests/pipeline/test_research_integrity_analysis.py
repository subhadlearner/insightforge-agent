"""ADR-0006 §5-§6: the Writer answers the requirements, and analytical Claims rest on all their
premises, verified together before publication, cited together, and never made Findings.

Scripted models only: these tests prove the contract's code paths, not the quality of a live
Writer's synthesis (which needs a live run and human review)."""

import json
import re

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import (
    Claim,
    HistoricalContext,
    ReportDraft,
    ReportSection,
)
from insightforge_agent.domain.models import Report
from insightforge_agent.domain.requirements import RequirementStatus
from insightforge_agent.pipeline.citations import claim_citations
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.write import violations, write_report
from tests.pipeline.test_t6_writer_fact_check import URL, Fetcher, evidence
from tests.scripted import ScriptedChat

BYD = "BYD sold 4.27 million vehicles in 2025."
TESLA = "Tesla sold 1.64 million vehicles in 2025."
COMPARISON = "BYD sold 4.27 million vehicles in 2025, more than Tesla's 1.64 million."
JOINT = "You check whether numbered passages, taken together"


def joint_judge(verdict="SUPPORTED") -> ScriptedChat:
    """Answers the joint support judgment; a SUPPORTED one maps each part of the comparison to
    the passage that states it, and any other statement (a summary sentence, judged against the
    published Claims) to every unit it was shown."""
    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        assert system.startswith(JOINT), system[:60]
        user = str(messages[-1].content)
        statement = user.split("\n", 1)[0].removeprefix("Statement: ")
        if statement == COMPARISON:
            parts = [{"part": "BYD sold 4.27 million vehicles in 2025",
                      "excerpts": [{"passage": 1, "text": BYD.rstrip(".")}]},
                     {"part": "more than Tesla's 1.64 million",
                      "excerpts": [{"passage": 2, "text": TESLA.rstrip(".")}]}]
        else:
            parts = [{"part": statement, "excerpts": [
                {"passage": int(n), "text": t}
                for n, t in re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE)]}]
        return AIMessage(content=json.dumps(
            {"verdict": verdict, "parts": parts if verdict == "SUPPORTED" else []}))
    return ScriptedChat(reply=reply)


def writer(*claims_per_attempt) -> ScriptedChat:
    """Replies with the given claim lists in turn (the last repeats)."""
    state = {"n": 0}

    def reply(messages):
        claims = claims_per_attempt[min(state["n"], len(claims_per_attempt) - 1)]
        state["n"] += 1
        return AIMessage(content=json.dumps({"title": "EV sales", "summary": BYD,
                                             "sections": [{"heading": "Sales",
                                                           "claims": claims}]}))
    return ScriptedChat(reply=reply)


ANALYTICAL = {"text": COMPARISON, "evidence_id": "ev_0", "premises": ["ev_1"]}


def world(make_deps, light, writer_model):
    deps = make_deps(light_model=light, writer_model=writer_model,
                     fetcher=Fetcher({URL: f"{BYD}\n\n{TESLA}"}))
    _, bundle = evidence(deps, [BYD, TESLA])
    return deps, bundle


def events(deps, type_):
    return [e.payload for e in deps.repos.events.read_after("alice", "run_1") if e.type == type_]


# ---- the Writer's grounding and premises ------------------------------------------------------

def test_an_analytical_claim_is_grounded_in_the_union_of_its_cited_items(make_deps):
    _, bundle = evidence(make_deps(), [BYD, TESLA])
    draft = ReportDraft(title="T", summary="", sections=[ReportSection(heading="Sales", claims=[
        Claim(**ANALYTICAL)])])
    assert violations(draft, bundle) == []
    lonely = draft.model_copy(update={"sections": [ReportSection(heading="Sales", claims=[
        Claim(text=COMPARISON, evidence_id="ev_0")])]})
    assert any("1.64" in p for p in violations(lonely, bundle))  # Tesla's figure needs its item


@pytest.mark.parametrize("claim, problem", [
    (Claim(text=COMPARISON, evidence_id="ev_0", premises=["ev_9"]), "not current evidence"),
    (Claim(text=COMPARISON + " Total 5.91 million.", evidence_id="ev_0", premises=["ev_1"]),
     "5.91"),  # a derived number neither item states
    (Claim(text="x (reported 2026-01-01).", evidence_id="hist_1", historical=True,
           premises=["ev_1"]), "cannot have premises"),
])
def test_premises_must_be_current_items_and_add_no_new_number(make_deps, claim, problem):
    _, bundle = evidence(make_deps(), [BYD, TESLA])
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[claim])])
    assert any(problem in p for p in violations(draft, bundle, HistoricalContext()))


# ---- verified before publication ----------------------------------------------------------

def test_a_supported_analytical_claim_is_published_with_its_premises(make_deps):
    deps, bundle = world(make_deps, joint_judge("SUPPORTED"), writer([ANALYTICAL]))
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [])
    (claim,) = [c for s in draft.sections for c in s.claims]
    assert claim.premises == ["ev_1"] and draft.quality == []
    judged, summary = events(deps, "support_joint_judged")  # the Claim, then the summary
    assert judged["units"] == 2 and judged["judgment"]["verdict"] == "SUPPORTED"
    assert summary["summary_sentence"] == 0 and summary["judgment"]["verdict"] == "SUPPORTED"
    request = str(deps.light_model.seen[0][-1].content)
    assert BYD in request and TESLA in request  # both premises, their own Passages only


def test_an_analytical_claim_its_premises_do_not_support_is_removed_and_flagged(make_deps):
    deps, bundle = world(make_deps, joint_judge("INSUFFICIENT_EVIDENCE"), writer([ANALYTICAL]))
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [])
    claims = [c for s in draft.sections for c in s.claims]
    assert all(not c.premises for c in claims) and claims  # the section was refilled
    (rejected,) = events(deps, "analytical_claim_rejected")
    assert rejected["rejected"][0]["evidence_ids"] == ["ev_0", "ev_1"]
    assert "analytical_claims_removed:1" in draft.quality
    assert "sections_filled_verbatim:1" in draft.quality  # degraded output, said so


# ---- requirements answered -----------------------------------------------------------------

STATUSES = [RequirementStatus(id="R1", question="BYD 2025 sales?", priority="required",
                              status="supported", evidence_ids=["ev_0"]),
            RequirementStatus(id="R2", question="Tesla 2025 sales?", priority="required",
                              status="supported", evidence_ids=["ev_1"])]


def test_an_unanswered_requirement_is_sent_back_to_the_writer(make_deps):
    first = [{"text": BYD, "evidence_id": "ev_0"}]
    deps, bundle = world(make_deps, joint_judge(), writer(first, first + [
        {"text": TESLA, "evidence_id": "ev_1"}]))
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [], statuses=STATUSES)
    assert {c.evidence_id for s in draft.sections for c in s.claims} == {"ev_0", "ev_1"}
    (retry,) = events(deps, "writer_violations")
    assert any("requirement R2 (Tesla 2025 sales?)" in p for p in retry["problems"])
    prompt = str(deps.writer_model.seen[0][-1].content)
    assert "- R2 [required] Tesla 2025 sales?: answered by ev_1" in prompt
    assert draft.quality == []


def test_when_retries_run_out_repair_answers_it_verbatim_and_says_so(make_deps):
    deps, bundle = world(make_deps, joint_judge(), writer([{"text": BYD, "evidence_id": "ev_0"}]))
    draft = write_report(deps, "alice", "run_1", "EV sales", bundle, [], statuses=STATUSES)
    assert TESLA in [c.text for s in draft.sections for c in s.claims]
    assert "requirements_filled_verbatim:R2" in draft.quality


# ---- checked again, cited together, never a Finding ---------------------------------------

def test_a_sampled_analytical_claim_is_checked_against_all_its_premises(make_deps):
    deps, bundle = world(make_deps, joint_judge("SUPPORTED"), None)
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(**ANALYTICAL)])])
    (v,) = fact_check(deps, "alice", "run_1", draft, bundle).verdicts
    assert (v.check, v.verdict) == ("premises_joint", "verified")
    deps2, bundle2 = world(make_deps, joint_judge("CONTRADICTED"), None)
    (v2,) = fact_check(deps2, "alice", "run_1", draft, bundle2).verdicts
    assert v2.verdict == "unverified" and "CONTRADICTED" in v2.reason


def test_the_citation_drawer_resolves_every_premise(make_deps):
    deps, bundle = world(make_deps, joint_judge(), None)
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(**ANALYTICAL)])])
    report = Report(id="rep_1", owner_id="alice", run_id="run_1", created_at=deps.now(),
                    body={"draft": draft.model_dump(mode="json"),
                          "evidence": bundle.model_dump(mode="json")})
    (found,) = claim_citations(deps.store, "alice", report)
    assert found.citation.passage_text == BYD
    assert [p.passage_text for p in found.citation.premises] == [TESLA]
    assert found.citation.premises[0].evidence_id == "ev_1"


def test_an_analytical_claim_never_seeds_a_finding(make_deps):
    from insightforge_agent.domain.contracts import VerifiedReport
    from insightforge_agent.scheduler.watchlist import report_seeds

    deps, bundle = world(make_deps, joint_judge(), None)
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=BYD, evidence_id="ev_0"), Claim(**ANALYTICAL)])])
    report = Report(id="rep_1", owner_id="alice", run_id="run_1", created_at=deps.now(),
                    body={"draft": draft.model_dump(mode="json"),
                          "verified": VerifiedReport(draft=draft).model_dump(mode="json"),
                          "evidence": bundle.model_dump(mode="json")})
    (seed,) = report_seeds(deps, "alice", report)
    assert seed.claim.text == BYD and seed.claim.premises == []


def test_the_export_cites_every_source_an_analytical_claim_rests_on(make_deps):
    from insightforge_agent.domain.contracts import VerifiedReport
    from insightforge_agent.pipeline.report_delivery import markdown_export

    deps = make_deps(light_model=joint_judge(), writer_model=None)
    _, a = evidence(deps, [BYD], url="https://a.example.com/byd")
    obs_b, b = evidence(deps, [TESLA], url="https://b.example.org/tesla")
    tesla = b.sections[0].items[0].model_copy(update={"id": "ev_t"})
    bundle = a.model_copy(update={"sections": [a.sections[0].model_copy(
        update={"items": [a.sections[0].items[0], tesla]})]})
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=COMPARISON, evidence_id="ev_0", premises=["ev_t"])])])
    report = Report(id="rep_1", owner_id="alice", run_id="run_1", created_at=deps.now(),
                    body={"draft": draft.model_dump(mode="json"),
                          "verified": VerifiedReport(draft=draft).model_dump(mode="json"),
                          "evidence": bundle.model_dump(mode="json")})
    text = markdown_export(report, deps.store, "alice")
    (line,) = [x for x in text.splitlines() if x.startswith(f"- {COMPARISON}")]
    assert "a.example.com" in line and "b.example.org" in line
    assert "https://b.example.org/tesla" in text  # listed under Sources too


def test_an_analytical_claim_never_becomes_background_for_a_later_run(make_deps):
    from insightforge_agent.domain.contracts import VerifiedReport
    from insightforge_agent.pipeline.memory import claim_lineage

    deps, bundle = world(make_deps, joint_judge(), None)
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=BYD, evidence_id="ev_0"), Claim(**ANALYTICAL)])])
    report = Report(id="rep_1", owner_id="alice", run_id="run_1", created_at=deps.now(),
                    body={"draft": draft.model_dump(mode="json"),
                          "verified": VerifiedReport(draft=draft).model_dump(mode="json"),
                          "evidence": bundle.model_dump(mode="json")})
    assert claim_lineage(report, 0, 0) is not None
    assert claim_lineage(report, 0, 1) is None
