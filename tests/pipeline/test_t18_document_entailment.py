"""T18 Stage C: an uploaded company document's own identity in the entailment judge.

The Stage C rerun (run_61c2b5c7ec63) dropped "Northwind Health research and development spending
was 71 million dollars in fiscal 2025." and the clinical-programmes item: the literal check
credited "Northwind Health" to the document (T18 CS2), but the judge was asked about the whole
statement against a Passage that never names the company. Now a credited Passage is judged on
the statement without the credited company names; everything else is judged as before. The
scripted judge below is strict: SUPPORTED only when the Passage states the statement as given."""

import json

from insightforge_agent.domain.contracts import ExtractionResult
from insightforge_agent.pipeline.synthesize import synthesize
from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline.test_extraction_synthesis import BRIEF, NOW, OWNER, RUN, light, world  # noqa: F401

REPORT = "northwind-health-annual-report-2025.pdf"
CHUNKS = ["Northwind Health revenue reached 612 million dollars in fiscal 2025.",
          "Research and development spending was 71 million dollars in fiscal 2025.",
          "The pipeline holds 6 programmes in clinical trials at the end of fiscal 2025.",
          "Northwind Health employed 2,140 people at the end of fiscal 2025."]
RND = "Northwind Health research and development spending was 71 million dollars in fiscal 2025."
PROGRAMMES = ("The Northwind Health pipeline holds 6 programmes in clinical trials at the end of "
              "fiscal 2025.")


def strict(ps, user):
    """SUPPORTED only where the Passage states the judged statement itself."""
    statement = user.split("\n", 1)[0].removeprefix("Statement: ").rstrip(".").casefold()
    return [{"passage": n, "verdict": "SUPPORTED" if statement in t.casefold()
             else "INSUFFICIENT_EVIDENCE"} for n, t in ps]


def drafts(*cited):
    """A draft handler: each (statement, chunk) drafted from the shown Passage holding `chunk`."""
    def handler(ps, user):
        return [{"statement": s, "passages": [n for n, t in ps if t == chunk],
                 "entities": ["Northwind Health"] if "Northwind" in s else [],
                 "entity": "Northwind Health" if "Northwind" in s else ""} for s, chunk in cited]
    return handler


def run(world, chunks, draft, entail=strict, kind="pdf"):
    if kind == "pdf":
        obs = world.deps.store.ingest_document(
            owner_id=OWNER, run_id=RUN, content_hash="nw", filename=REPORT,
            chunks=[PassageDraft(c, page=1) for c in chunks], fetched_at=NOW,
            credibility_score=0.7)
        world.sources["doc"] = obs.source_id
    else:
        world.page("doc", "\n\n".join(chunks))
    model = light(draft=draft, entail=entail)
    world.deps.light_model = model
    bundle = synthesize(world.deps, OWNER, RUN, BRIEF, world.plan(("s1", "a1", 1)),
                        world.research(s1=["doc"]), ExtractionResult())
    return [i.statement for i in world.items(bundle)], model


def test_the_rnd_and_programmes_items_are_kept_and_judged_without_the_company_name(world):
    kept, model = run(world, CHUNKS, drafts((RND, CHUNKS[1]), (PROGRAMMES, CHUNKS[2])))
    assert sorted(kept) == sorted([RND, PROGRAMMES])
    judged = model.calls("You judge whether")
    assert any(u.startswith("Statement: Research and development spending was 71 million")
               for u in judged)
    assert any(u.startswith("Statement: The pipeline holds 6 programmes") for u in judged)
    assert not any("Northwind" in u.split("\n", 1)[0] for u in judged)
    (rnd,) = [e for e in world.events("evidence_entailment")
              if e["passages"][0]["index"] == 1]
    assert {k: v for k, v in rnd["passages"][0].items() if k != "observation_id"} == {
        "index": 1, "verdicts": ["SUPPORTED"], "literal_gate": True, "document_credit": True}
    assert rnd["outcome"] == "supported" and rnd["subtask_id"] == "s1"


def test_the_credit_never_supplies_a_qualifier_the_passage_does_not_state(world):
    # "cardiology" is an ordinary word, so the literal check passes; the judge still sees it.
    wider = ("Northwind Health cardiology research and development spending was 71 million "
             "dollars in fiscal 2025.")
    kept, model = run(world, CHUNKS, drafts((wider, CHUNKS[1])))
    assert kept == []
    assert any(u.startswith("Statement: Cardiology research and development spending")
               for u in model.calls("You judge whether"))
    (dropped,) = world.events("evidence_dropped")
    assert dropped["reason"] == "no supported Passage"
    (diag,) = world.events("evidence_entailment")
    assert diag["outcome"] == "no supported Passage"
    assert diag["passages"][0]["verdicts"] == ["INSUFFICIENT_EVIDENCE"]


def test_company_credit_alone_never_makes_an_item_supported(world):
    kept, _ = run(world, CHUNKS, drafts((RND, CHUNKS[1])),
                  entail=lambda ps, u: [{"passage": n, "verdict": "INSUFFICIENT_EVIDENCE"}
                                        for n, _ in ps])
    assert kept == []


def test_a_passage_about_another_company_gets_no_credit(world):
    chunks = CHUNKS[:1] + ["Medtronic research and development spending was 71 million dollars "
                           "in fiscal 2025."] + CHUNKS[2:]
    kept, model = run(world, chunks, drafts((RND, chunks[1])))
    assert kept == [] and model.calls("You judge whether") == []
    (dropped,) = world.events("evidence_dropped")
    assert "Northwind" in str(dropped["rejected"])  # the literal check, before any judge


def test_web_sources_are_judged_on_the_statement_as_written(world):
    page = ["Northwind Health revenue reached 612 million dollars in fiscal 2025."]
    kept, model = run(world, page, drafts((page[0], page[0])), kind="web")
    assert kept == page
    (judged,) = model.calls("You judge whether")
    assert judged.startswith(f"Statement: {page[0]}")
    (diag,) = world.events("evidence_entailment")
    assert diag["passages"][0]["document_credit"] is False


def test_an_item_whose_passage_names_the_company_is_judged_as_written(world):
    kept, model = run(world, CHUNKS, drafts((CHUNKS[0], CHUNKS[0])))
    assert kept == [CHUNKS[0]]
    assert model.calls("You judge whether")[0].startswith(f"Statement: {CHUNKS[0]}")


def test_entailment_diagnostics_hold_references_and_verdicts_only(world):
    run(world, CHUNKS, drafts((RND, CHUNKS[1])))
    (diag,) = world.events("evidence_entailment")
    assert set(diag) == {"evidence_id", "subtask_id", "passages", "outcome"}
    assert set(diag["passages"][0]) == {"observation_id", "index", "verdicts", "literal_gate",
                                        "document_credit"}
    assert not any(c in json.dumps(diag) for c in CHUNKS)  # no Passage text
