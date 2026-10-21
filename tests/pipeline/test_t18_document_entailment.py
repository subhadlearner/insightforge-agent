"""T18 Stage C: an uploaded company document's own identity in the support judgment.

The Stage C rerun (run_61c2b5c7ec63) dropped "Northwind Health research and development spending
was 71 million dollars in fiscal 2025." and the clinical-programmes item, and the final attempt
lost the programmes item to the lexical gate. Since ADR-0006 no name rule runs before the judge,
and no name is removed from the statement: the judge sees the statement as written, with identity
context saying only which company the uploaded document speaks for. The scripted judge below
follows the contract: it may take the document's company from identity, and every other part of
the statement only from words the Passage states."""

import json

from tests.pipeline.test_extraction_synthesis import (  # noqa: F401
    NOW,
    OWNER,
    RUN,
    light,
    statement_of,
    world,
)
from insightforge_agent.stores.source_store import PassageDraft

REPORT = "northwind-health-annual-report-2025.pdf"
CHUNKS = ["Northwind Health revenue reached 612 million dollars in fiscal 2025.",
          "Research and development spending was 71 million dollars in fiscal 2025.",
          "The pipeline holds 6 programmes in clinical trials at the end of fiscal 2025.",
          "Northwind Health employed 2,140 people at the end of fiscal 2025."]
RND = "Northwind Health research and development spending was 71 million dollars in fiscal 2025."
PROGRAMMES = ("The Northwind Health pipeline holds 6 programmes in clinical trials at the end of "
              "fiscal 2025.")


def strict(ps, user):
    """SUPPORTED only where the Passage states the rest of the statement word for word; the
    company may come from identity context when the request gives it for that Passage."""
    statement = statement_of(user).rstrip(".")
    out = []
    for n, t in ps:
        # Identity never applies to a Passage that gives the fact to someone else.
        identity = (f"Passage [{n}] is from an uploaded document of Northwind Health" in user
                    and not t.startswith("Medtronic"))
        rest = statement
        for name in ("The Northwind Health ", "Northwind Health "):
            if identity and rest.startswith(name):
                rest = rest.removeprefix(name)
        found = rest.casefold() in t.casefold()
        parts = ([{"part": "Northwind Health", "identity": True}] if rest != statement else [])
        out.append({"passage": n, "verdict": "SUPPORTED" if found else "INSUFFICIENT_EVIDENCE",
                    **({"parts": parts + [{"part": rest, "excerpts": [rest]}]} if found else {})})
    return out


def drafts(*cited):
    """An extraction handler: each (statement, chunk) proposed for the shown Passage holding
    `chunk`, with the compound entity label the live model used."""
    def handler(ps, user):
        return [{"statement": s, "passages": [n for n, t in ps if t == chunk],
                 "entities": ["Northwind Health"] if "Northwind" in s else [],
                 "entity": "Northwind Health pipeline" if "pipeline" in s else
                 ("Northwind Health" if "Northwind" in s else "")} for s, chunk in cited]
    return handler


def run(world, chunks, draft, support=strict, kind="pdf"):
    if kind == "pdf":
        obs = world.deps.store.ingest_document(
            owner_id=OWNER, run_id=RUN, content_hash="nw", filename=REPORT,
            chunks=[PassageDraft(c, page=1) for c in chunks], fetched_at=NOW,
            credibility_score=0.7)
        world.sources["doc"] = obs.source_id
    else:
        world.page("doc", "\n\n".join(chunks))
    model = light(draft=draft, support=support)
    world.deps.light_model = model
    bundle = world.synth(world.plan(("s1", "a1", 1)), world.research(s1=["doc"]))
    return [i.statement for i in world.items(bundle)], model


def test_the_rnd_and_programmes_items_are_kept_and_judged_as_written(world):
    kept, model = run(world, CHUNKS, drafts((RND, CHUNKS[1]), (PROGRAMMES, CHUNKS[2])))
    assert sorted(kept) == sorted([RND, PROGRAMMES])
    judged = model.calls("You check whether")
    assert {u.split("\n", 1)[0] for u in judged} == {f"Statement: {RND}",
                                                       f"Statement: {PROGRAMMES}"}
    assert all("is from an uploaded document of Northwind Health" in u for u in judged)
    (rnd,) = [e for e in world.events("evidence_support") if e["passages"][0]["index"] == 1]
    assert {k: v for k, v in rnd["passages"][0].items() if k != "observation_id"} == {
        "index": 1, "verdicts": ["SUPPORTED"], "figure_gate": True, "identity_context": True}
    assert rnd["outcome"] == "supported" and rnd["subtask_id"] == "s1"


def test_identity_never_supplies_a_qualifier_the_passage_does_not_state(world):
    wider = ("Northwind Health cardiology research and development spending was 71 million "
             "dollars in fiscal 2025.")
    kept, model = run(world, CHUNKS, drafts((wider, CHUNKS[1])))
    assert kept == []
    (dropped,) = world.events("evidence_dropped")
    assert dropped["reason"] == "no supported Passage"
    (diag,) = world.events("evidence_support")
    assert diag["passages"][0]["verdicts"] == ["INSUFFICIENT_EVIDENCE"]


def test_identity_alone_never_makes_an_item_supported(world):
    kept, _ = run(world, CHUNKS, drafts((RND, CHUNKS[1])),
                  support=lambda ps, u: [{"passage": n, "verdict": "INSUFFICIENT_EVIDENCE"}
                                         for n, _ in ps])
    assert kept == []


def test_a_passage_about_another_company_is_judged_and_not_given_to_the_document(world):
    chunks = CHUNKS[:1] + ["Medtronic research and development spending was 71 million dollars "
                           "in fiscal 2025."] + CHUNKS[2:]
    kept, model = run(world, chunks, drafts((RND, chunks[1])))
    assert kept == [] and len(model.calls("You check whether")) == 1  # judged, never pre-refused
    (dropped,) = world.events("evidence_dropped")
    assert dropped["reason"] == "no supported Passage"


def test_web_sources_get_no_identity_context(world):
    page = ["Northwind Health revenue reached 612 million dollars in fiscal 2025."]
    kept, model = run(world, page, drafts((page[0], page[0])), kind="web")
    assert kept == page
    (judged,) = model.calls("You check whether")
    assert judged.startswith(f"Statement: {page[0]}") and "Identity context" not in judged
    (diag,) = world.events("evidence_support")
    assert diag["passages"][0]["identity_context"] is False


def test_an_item_whose_passage_names_the_company_is_judged_as_written(world):
    kept, model = run(world, CHUNKS, drafts((CHUNKS[0], CHUNKS[0])))
    assert kept == [CHUNKS[0]]
    assert model.calls("You check whether")[0].startswith(f"Statement: {CHUNKS[0]}")


def test_support_diagnostics_hold_references_and_verdicts_only(world):
    run(world, CHUNKS, drafts((RND, CHUNKS[1])))
    (diag,) = world.events("evidence_support")
    assert set(diag) == {"evidence_id", "subtask_id", "passages", "outcome"}
    assert set(diag["passages"][0]) == {"observation_id", "index", "verdicts", "figure_gate",
                                        "identity_context"}
    assert not any(c in json.dumps(diag) for c in CHUNKS)  # no Passage text
