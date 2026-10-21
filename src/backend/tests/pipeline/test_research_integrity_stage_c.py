"""The Stage C document fixture, offline (ADR-0006 exit criterion 1).

Every stage after research runs as production code (Extraction, Synthesis, the Writer, the
Fact-Checker and the requirements ledger) over the uploaded Northwind annual report as T18
stored it (11 chunks). The models are scripted to behave correctly, using the live model's own
wording where T18 recorded it ("The Northwind Health pipeline holds 6 programmes ...", labelled
"Northwind Health pipeline"; risks restated with the company). The oracle is independent of
production code: the 13 facts established for this document, each a hand-written sentence that
a published Claim must state and whose cited Passage must contain the source sentence.

What this proves: no stage loses, misattributes or leaves unreported any of the 13 facts when
the models do their part, and the one thing the run could not research (the web-only market
comparison) is reported as a limitation. What it cannot prove: that a live model does its part.
Before ADR-0006 the six-programmes fact was lost here by the lexical gate whatever the judge said
(tests/pipeline/test_research_integrity_synthesis.py reproduces that loss)."""

import json
import re

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import (
    PassageRef,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.requirements import (
    Requirement,
    RequirementLedger,
    assess,
    gap_lines,
    mark_reported,
)
from insightforge_agent.pipeline.extract import extract
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.synthesize import synthesize
from insightforge_agent.pipeline.write import research_gaps, write_report
from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline.test_t18_document_recovery import NORTHWIND
from tests.scripted import ScriptedChat

OWNER, RUN = "alice", "run_c"
BRIEF = ("Using the uploaded Northwind Health 2025 annual report, assess the company's revenue "
         "and segment performance, its research and development pipeline, and its principal "
         "risks, and compare its growth with the wider medical devices market.")

# The oracle: (fact, the sentence a published Claim states, the source sentence its cited
# Passage must contain). Written from the sample PDF, not from production output.
FACTS = [
    ("revenue 612 million", "Northwind Health revenue reached 612 million dollars in fiscal 2025.",
     "revenue reached 612 million dollars in fiscal 2025"),
    ("growth 14 percent", "Northwind Health revenue grew 14 percent compared with fiscal 2024, "
     "led by the cardiology segment.", "Revenue grew 14 percent compared with fiscal 2024"),
    ("cardiology 341 million", "Cardiology devices contributed 341 million dollars of revenue "
     "in fiscal 2025.", "Cardiology devices contributed 341 million dollars"),
    ("orthopaedic 187 million", "Orthopaedic implants contributed 187 million dollars of revenue "
     "in fiscal 2025.", "Orthopaedic implants contributed 187 million dollars"),
    ("diagnostics 84 million", "Diagnostics contributed 84 million dollars of revenue in fiscal "
     "2025.", "Diagnostics contributed 84 million dollars"),
    ("R&D 71 million", "Northwind Health research and development spending was 71 million "
     "dollars in fiscal 2025.", "Research and development spending was 71 million dollars"),
    ("6 programmes", "The Northwind Health pipeline holds 6 programmes in clinical trials at the "
     "end of fiscal 2025.", "The pipeline holds 6 programmes in clinical trials"),
    ("NW-200 pivotal trial", "The NW-200 heart valve completed its pivotal trial in September "
     "2025.", "completed its pivotal trial in September 2025"),
    ("NW-200 approval expected H1 2026", "Northwind expects regulatory approval for NW-200 in "
     "the first half of 2026.", "expects regulatory approval for NW-200 in the first half of 2026"),
    ("titanium supplier", "A single supplier provides the titanium used in Northwind Health's "
     "orthopaedic implants.", "A single supplier provides the titanium"),
    ("hospital pricing pressure", "Pricing pressure from hospital purchasing groups reduced "
     "Northwind Health's average selling prices.", "Pricing pressure from hospital purchasing"),
    ("NW-200 delay", "Delays to the NW-200 approval would defer Northwind Health's expected "
     "cardiology revenue.", "Delays to the NW-200 approval would defer expected cardiology"),
    ("outlook 9 to 11 percent", "Northwind Health expects fiscal 2026 revenue growth of 9 to 11 "
     "percent.", "expects fiscal 2026 revenue growth of 9 to 11 percent"),
]
PROPOSED = {source: claim for _, claim, source in FACTS}

PLAN = SubTaskPlan(
    aspects=[Aspect(id="a1", name="Revenue and segment performance"),
             Aspect(id="a2", name="Research and development pipeline"),
             Aspect(id="a3", name="Principal risks"), Aspect(id="a4", name="Market comparison")],
    sub_tasks=[SubTask(id="s1", aspect_id="a1", query="revenue segments outlook",
                       source_type="documents", time_horizon_months=12, priority=1),
               SubTask(id="s2", aspect_id="a2", query="R&D pipeline programmes NW-200",
                       source_type="documents", time_horizon_months=12, priority=2),
               SubTask(id="s3", aspect_id="a3", query="principal risks",
                       source_type="documents", time_horizon_months=12, priority=3),
               SubTask(id="s4", aspect_id="a4", query="medical devices market growth 2025",
                       source_type="web", time_horizon_months=12, priority=4)])
LEDGER = RequirementLedger(origin="brief", requirements=[
    Requirement(id="R1", question="Revenue and growth in fiscal 2025?", aspect_ids=["a1"]),
    Requirement(id="R2", question="Segment revenues?", aspect_ids=["a1"]),
    Requirement(id="R3", question="R&D spending and pipeline?", aspect_ids=["a2"]),
    Requirement(id="R4", question="Principal risks?", aspect_ids=["a3"]),
    # A comparison is one requirement per side: Northwind's growth is R1, the market's is R5.
    Requirement(id="R5", question="Growth of the wider medical devices market in 2025?",
                aspect_ids=["a4"])])
LINKS = {"612": ["R1"], "14 percent": ["R1"], "341": ["R2"], "187": ["R2"],
         "84 million": ["R2"], "9 to 11": ["R1"], "71 million": ["R3"], "programmes": ["R3"],
         "pivotal": ["R3"], "approval for NW-200": ["R3"], "titanium": ["R4"], "Pricing": ["R4"],
         "Delays": ["R4"]}
SCOPE = {"s1": [0, 1, 2, 10], "s2": [3, 4, 5, 6], "s3": [9]}  # the chunks each Reader returned


def sentences(text):
    return [s for s in re.split(r"(?<=\.) (?=[A-Z])", text) if s]


def extraction_reply(user):
    """One fact per stated sentence, worded as the live model worded it, linked to the
    requirements it answers."""
    facts = []
    for n, text in re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE):
        for s in sentences(text):
            claim = next((c for src, c in PROPOSED.items() if src in s), None)
            if claim is None:
                continue  # operating margin, employees, Cork: not asked about
            facts.append({"passage_index": int(n), "statement": claim,
                          "entity": "Northwind Health pipeline" if "pipeline" in claim
                          else "Northwind Health",
                          "entities": ["Northwind Health"],
                          "requirements": next(v for k, v in LINKS.items() if k in s)})
    return {"facts": facts}


COMPANY = ("The Northwind Health ", "Northwind Health's ", "Northwind Health ", "Northwind ")


def support_reply(user):
    """A correct judge: the company may come from identity context; everything else must be
    in the Passage's own words."""
    statement = user.split("\n", 1)[0].removeprefix("Statement: ").rstrip(".")
    out = []
    for n, text in re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE):
        identity = f"Passage [{n}] is from an uploaded document of Northwind Health" in user
        rest, named = statement, []
        if identity:  # the sentence's subject may be unnamed even where the chunk names it
            for name in COMPANY:
                if name in rest:
                    rest = rest.replace(name, "", 1)
                    named.append(name.strip())
        rest = " ".join(rest.split())
        body = rest[0].upper() + rest[1:] if rest else rest
        found = next((x for x in (rest, body) if x and x.casefold() in text.casefold()), None)
        if found is None:
            out.append({"passage": int(n), "verdict": "INSUFFICIENT_EVIDENCE"})
            continue
        parts = [{"part": nm, "identity": True} for nm in named]
        out.append({"passage": int(n), "verdict": "SUPPORTED",
                    "parts": parts + [{"part": rest, "excerpts": [found]}]})
    return {"verdicts": out}


def light():
    def reply(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        if system.startswith("You extract facts"):
            return AIMessage(content=json.dumps(extraction_reply(user)))
        if system.startswith("You check whether numbered passages, taken together"):
            return AIMessage(content=json.dumps({"verdict": "SUPPORTED", "parts": [
                {"part": "Northwind Health revenue reached 612 million dollars in fiscal 2025",
                 "excerpts": [{"passage": 1, "text": "Northwind Health revenue reached 612 "
                                                     "million dollars in fiscal 2025"}]},
                {"part": "of which cardiology devices contributed 341 million dollars",
                 "excerpts": [{"passage": 2, "text": "Cardiology devices contributed 341 "
                                                     "million dollars"}]}]}))
        if system.startswith("You check whether"):
            return AIMessage(content=json.dumps(support_reply(user)))
        return AIMessage(content="{}")
    return ScriptedChat(reply=reply)


ANALYSIS = ("Northwind Health revenue reached 612 million dollars in fiscal 2025, of which "
            "cardiology devices contributed 341 million dollars.")


def writer():
    """Cites every item (one Claim each, in its section) and adds one analytical Claim."""
    def reply(messages):
        user = str(messages[-1].content)
        items = re.findall(r"- \[(ev_\w+)\] \([^)]*\) (.+)", user)
        ids = {t: e for e, t in items}
        claims = [{"text": t, "evidence_id": e} for e, t in items]
        revenue, cardiology = ids.get(FACTS[0][1]), ids.get(FACTS[2][1])
        if revenue and cardiology:
            claims.append({"text": ANALYSIS, "evidence_id": revenue, "premises": [cardiology]})
        return AIMessage(content=json.dumps({
            "title": "Northwind Health 2025", "summary": FACTS[0][1],
            "sections": [{"heading": "Findings", "claims": claims}]}))
    return ScriptedChat(reply=reply)


def run_stage_c(make_deps):
    deps = make_deps(light_model=light(), writer_model=writer())
    obs = deps.store.ingest_document(
        owner_id=OWNER, run_id=RUN, content_hash="nw",
        filename="northwind-health-annual-report-2025.pdf",
        chunks=[PassageDraft(t, page=p, section_heading=h) for p, h, t in NORTHWIND],
        fetched_at=deps.now(), credibility_score=0.7)
    research = ResearchResults(results=[
        SubTaskResult(subtask_id=sid, status="succeeded", source_refs=[SourceRef(
            source_id=obs.source_id, title="Northwind", summary="chunks",
            passages=[PassageRef(observation_id=obs.id, index=i) for i in idx])])
        for sid, idx in SCOPE.items()] + [SubTaskResult(subtask_id="s4", status="failed")])
    scope = research.passage_scope()
    extraction = extract(deps, OWNER, RUN, list(scope), scope, plan=PLAN, research=research,
                         ledger=LEDGER)
    bundle = synthesize(deps, OWNER, RUN, BRIEF, PLAN, research, extraction, LEDGER)
    statuses = assess(LEDGER, PLAN, research, extraction,
                      [i for s in bundle.sections for i in s.items])
    gaps = research_gaps(plan=PLAN, research=research, bundle=bundle, events=[],
                         documents=[], extraction=extraction) + gap_lines(statuses)
    draft = write_report(deps, OWNER, RUN, BRIEF, bundle, gaps, statuses=statuses)
    verified = fact_check(deps, OWNER, RUN, draft, bundle)
    return deps, bundle, mark_reported(statuses, draft), verified


def test_all_13_northwind_facts_are_captured_attributed_and_reported(make_deps):
    deps, bundle, _, verified = run_stage_c(make_deps)
    items = {i.id: i for s in bundle.sections for i in s.items}
    claims = [c for s in verified.draft.sections for c in s.claims]

    def cited_text(claim):
        return " ".join(deps.store.get_passages(OWNER, items[e].primary.observation_id,
                                                [items[e].primary.index])[0].text
                        for e in claim.evidence_ids())

    missing = [name for name, sentence, source in FACTS
               if not any(c.text.removesuffix(" [UNVERIFIED]") == sentence
                          and source in cited_text(c) for c in claims)]
    assert missing == []
    assert verified.draft.quality == []  # no degraded-output repair was needed


def test_one_cited_multi_evidence_comparison_is_published_and_cited_by_both_premises(make_deps):
    deps, bundle, _, verified = run_stage_c(make_deps)
    (analysis,) = [c for s in verified.draft.sections for c in s.claims if c.premises]
    assert analysis.text == ANALYSIS and len(analysis.evidence_ids()) == 2
    judged = [e.payload for e in deps.repos.events.read_after(OWNER, RUN)
              if e.type == "support_joint_judged"]
    assert judged and judged[0]["judgment"]["verdict"] == "SUPPORTED"


def test_the_unresearched_market_comparison_is_an_honest_limitation(make_deps):
    _, _, statuses, verified = run_stage_c(make_deps)
    by_id = {s.id: s for s in statuses}
    assert {k: by_id[k].status for k in ("R1", "R2", "R3", "R4")} == dict.fromkeys(
        ("R1", "R2", "R3", "R4"), "reported")
    assert by_id["R5"].status == "incomplete"
    gaps = verified.draft.gaps
    assert ("Requirement R5 (Growth of the wider medical devices market in 2025?): this research "
            "did not establish it; the research for it did not complete (Sub-task(s) s4 failed)."
            ) in gaps
    assert any(g.startswith("Sub-task s4 (medical devices market growth 2025)") for g in gaps)
    assert not any("no gaps" in g.lower() or "does not disclose" in g.lower() for g in gaps)


def test_the_sampled_fact_check_verifies_on_the_stored_chunks(make_deps):
    _, _, _, verified = run_stage_c(make_deps)
    assert verified.summary["sampled"] >= 5 and verified.summary["unverified"] == 0
    assert {v.check for v in verified.verdicts} <= {"document_passages", "premises_joint"}
