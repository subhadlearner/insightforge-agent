"""ADR-0006 in Synthesis, on the Stage C document trace (docs/t18-verification.md 2.3).

The final Stage C Run drafted "The Northwind Health pipeline holds 6 programmes ..." from the
uploaded annual report with the entity label "Northwind Health pipeline", and the T18 lexical
gate dropped it before any judge saw it. Here the same candidate goes through `synthesize` with
an honest scripted judge built from the gold set; the item must survive. The remaining tests pin
what the judge is shown and that code still refuses what it can refuse on its own.

Scripted models only: these tests prove the code path, not a live model's judgment."""

import json
from pathlib import Path

from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline.test_extraction_synthesis import (  # noqa: F401
    NOW,
    OWNER,
    RUN,
    light,
    statement_of,
    world,
)

GOLD = {c["id"]: c for c in json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "research_integrity"
     / "support_cases.json").read_text(encoding="utf-8"))["cases"]}
REPORT = "northwind-health-annual-report-2025.pdf"
CHUNKS = ["Northwind Health revenue reached 612 million dollars in fiscal 2025.",
          "Research and development spending was 71 million dollars in fiscal 2025.",
          "The pipeline holds 6 programmes in clinical trials at the end of fiscal 2025.",
          "Northwind Health employed 2,140 people at the end of fiscal 2025."]
SIX = GOLD["NW-P01"]


def candidates(*cases):
    """Answers drafting and extraction alike: each gold case's statement, with its own entity
    labels, cited to the shown Passage holding its text."""
    def handler(ps, user):
        out = []
        for case in cases:
            for n, t in ps:
                if t == case["passage"]:
                    out.append({"statement": case["statement"], "passages": [n],
                                "passage_index": n, "entities": case["entities"],
                                "entity": case["entity"]})
        return out
    return handler


def honest(ps, user):
    """A correct judge: the gold verdict and map for a gold (statement, passage) pair, and
    INSUFFICIENT_EVIDENCE for anything else."""
    statement = statement_of(user)
    out = []
    for n, t in ps:
        case = next((c for c in GOLD.values()
                     if c["statement"] == statement and c["passage"] == t), None)
        if case is None:
            out.append({"passage": n, "verdict": "INSUFFICIENT_EVIDENCE"})
        else:
            out.append({"passage": n, "verdict": case["expected"],
                        "parts": case.get("honest_map", [])})
    return out


def run(world, chunks, make, judge=honest, kind="pdf"):
    if kind == "pdf":
        obs = world.deps.store.ingest_document(
            owner_id=OWNER, run_id=RUN, content_hash="nw", filename=REPORT,
            chunks=[PassageDraft(c, page=1) for c in chunks], fetched_at=NOW,
            credibility_score=0.7)
        world.sources["doc"] = obs.source_id
    else:
        world.page("doc", "\n\n".join(chunks), title="Northwind Health market notes")
    model = light(extract_=make, support=judge)
    world.deps.light_model = model
    bundle = world.synth(world.plan(("s1", "a1", 1)), world.research(s1=["doc"]))
    return [i.statement for i in world.items(bundle)], model


def test_the_stage_c_six_programmes_fact_survives_with_its_live_entity_label(world):
    kept, _ = run(world, CHUNKS, candidates(SIX))
    assert kept == [SIX["statement"]]


def test_identity_context_names_the_document_entity_and_nothing_else(world):
    _, model = run(world, CHUNKS, candidates(SIX))
    (request,) = model.calls("You check whether")
    assert request.startswith(f"Statement: {SIX['statement']}")
    assert "- Passage [1] is from an uploaded document of Northwind Health." in request
    assert REPORT not in request and "Annual Report" not in request  # never the file or title
    assert [line for line in request.splitlines() if line.startswith("[")] == [
        f"[1] {SIX['passage']}"]  # the cited Passage only, no neighbour


def test_web_text_gets_no_identity_context_and_no_title(world):
    case = GOLD["WEB-P01"]
    kept, model = run(world, [case["passage"]], candidates(case), kind="web")
    assert kept == [case["statement"]]
    (request,) = model.calls("You check whether")
    assert "Identity context" not in request and "market notes" not in request


def test_identity_used_for_another_company_is_refused_by_code(world):
    medtronic = GOLD["NW-N02"]

    def lying(ps, user):
        return [{"passage": n, "verdict": "SUPPORTED", "parts": [
            {"part": "Medtronic", "identity": True},
            {"part": "pipeline holds 6 programmes in clinical trials at the end of fiscal 2025",
             "excerpts": [t]}]} for n, t in ps]

    kept, _ = run(world, CHUNKS, candidates(medtronic), judge=lying)
    assert kept == []
    (judged,) = world.events("support_judged")
    assert judged["judgments"][0]["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert "does not quote the document's entity" in judged["judgments"][0]["problems"][0]


def test_a_map_that_leaves_out_a_qualifier_is_not_support(world):
    wider = GOLD["NW-N05"]

    def lazy(ps, user):
        return [{"passage": n, "verdict": "SUPPORTED", "parts": [
            {"part": "Northwind Health", "identity": True},
            {"part": "research and development spending was 71 million dollars in fiscal 2025",
             "excerpts": [t]}]} for n, t in ps]

    kept, _ = run(world, CHUNKS, candidates(wider), judge=lazy)
    assert kept == []
    (judged,) = world.events("support_judged")
    assert judged["judgments"][0]["problems"] == ["no part covers ['cardiology']"]


def test_an_unusable_reply_is_a_recorded_verification_error_never_support(world):
    kept, model = run(world, CHUNKS, candidates(SIX), judge=lambda ps, user: "not a list")
    assert kept == []
    (judged,) = world.events("support_judged")
    assert judged["judgments"][0]["verdict"] == "VERIFICATION_ERROR"
    (dropped,) = world.events("evidence_dropped")
    assert dropped["reason"] == "verification incomplete: the support check failed"  # not "no supported Passage"
    assert dropped["verification_errors"] == 1 and dropped["unjudged"] == 1


def test_support_diagnostics_hold_references_offsets_and_reasons_only(world):
    run(world, CHUNKS, candidates(SIX))
    (judged,) = world.events("support_judged")
    record = judged["judgments"][0]
    assert record["verdict"] == "SUPPORTED" and record["identity"] == ["Northwind Health"]
    assert record["spans"] == [[0, len(SIX["passage"]) - 1]]  # the sentence without its full stop
    assert not any(c in json.dumps(judged) for c in CHUNKS)  # no Passage text
