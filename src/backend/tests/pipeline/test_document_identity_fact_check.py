"""T18 Stage C: the Fact-Checker uses the same source-identity rule as Synthesis (ADR-0006).

Before T18 Stage C, a current Claim "Northwind Health research and development spending was 71
million dollars in fiscal 2025." citing the uploaded Northwind report's Passage "Research and
development spending was 71 million dollars in fiscal 2025." was UNVERIFIED by a strict judge
only because the Passage does not name the company the document establishes. Now the Claim is
judged as written, with identity context naming the document's company; the judge below follows
the contract (the company from identity, every other part from the Passage's own words, never
identity for a Passage giving the fact to someone else)."""

import json
import re
from datetime import UTC, date, datetime

from langchain_core.messages import AIMessage

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
)
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline.test_writer_and_fact_check import draft_of
from tests.scripted import ScriptedChat

REPORT = "northwind-health-annual-report-2025.pdf"
CHUNKS = ["Northwind Health revenue reached 612 million dollars in fiscal 2025.",
          "Research and development spending was 71 million dollars in fiscal 2025.",
          "The pipeline holds 6 programmes in clinical trials at the end of fiscal 2025.",
          "Northwind Health employed 2,140 people at the end of fiscal 2025."]
RND = "Northwind Health research and development spending was 71 million dollars in fiscal 2025."


def strict() -> ScriptedChat:
    def reply(messages):
        user = str(messages[-1].content)
        statement = user.split("\n", 1)[0].removeprefix("Statement: ").rstrip(".")
        out = []
        for n, t in re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE):
            identity = (f"Passage [{n}] is from an uploaded document of Northwind Health" in user
                        and not t.startswith("Medtronic"))
            rest = statement
            for name in ("The Northwind Health ", "Northwind Health "):
                if identity and rest.startswith(name):
                    rest = rest.removeprefix(name)
            found = rest.casefold() in t.casefold()
            parts = [{"part": "Northwind Health", "identity": True}] if rest != statement else []
            out.append({"passage": int(n),
                        "verdict": "SUPPORTED" if found else "INSUFFICIENT_EVIDENCE",
                        **({"parts": parts + [{"part": rest, "excerpts": [rest]}]} if found else {})})
        return AIMessage(content=json.dumps({"verdicts": out}))
    return ScriptedChat(reply=reply)


def check(make_deps, claim, chunks=CHUNKS, cited=1, kind="pdf", model=None, historical=None):
    model = model or strict()
    deps = make_deps(light_model=model)
    if kind == "pdf":
        obs = deps.store.ingest_document(
            owner_id="alice", run_id="run_1", content_hash="nw", filename=REPORT,
            chunks=[PassageDraft(c, page=1) for c in chunks],
            fetched_at=datetime(2026, 10, 1, tzinfo=UTC), credibility_score=0.7)
    else:
        from tests.pipeline.test_writer_and_fact_check import URL, Fetcher
        deps = make_deps(light_model=model, fetcher=Fetcher({URL: "\n\n".join(chunks)}))
        obs = deps.store.ingest_web(
            owner_id="alice", run_id="run_1", url=URL, title="Northwind Health results",
            body="\n\n".join(chunks), fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
            credibility_score=0.8)
    ref = PassageRef(observation_id=obs.id, index=cited)
    bundle = EvidenceBundle(sections=[EvidenceSection(
        aspect_id="a1", heading="R&D", confidence="MEDIUM", items=[EvidenceItem(
            id="ev_0", subtask_id="s1", aspect_id="a1", statement=claim, confidence="MEDIUM",
            supporting=[ref], primary=ref, entity="Northwind Health")])])
    if historical:
        item = HistoricalItem(id="hist_1", subtask_id="m1", aspect_id="a9", heading="R&D",
                              statement=claim, entity="Northwind Health", report_id="rep_old",
                              run_id="run_old", report_date=date(2026, 1, 5), primary=ref)
        draft = draft_of([f"{claim[:-1]} (reported 2026-01-05)."], ["hist_1"], historical=(0,))
        result = fact_check(deps, "alice", "run_1", draft, bundle, HistoricalContext(items=[item]))
    else:
        result = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle)
    (verdict,) = result.verdicts
    judged = [str(m[-1].content) for m in model.seen]
    events = [e.payload for e in deps.repos.events.read_after("alice", "run_1")
              if e.type == "fact_check_support"]
    return verdict, judged, events


IDENTITY = "is from an uploaded document of Northwind Health"


def test_a_document_claim_naming_the_documents_company_is_verified_on_its_fact(make_deps):
    v, judged, events = check(make_deps, RND)
    assert v.verdict == "verified" and v.check == "document_passages"
    (request,) = judged
    assert request.startswith(f"Statement: {RND}") and IDENTITY in request  # as written
    assert events[0]["cited_judged"] is True


def test_the_programmes_pattern_is_verified_the_same_way(make_deps):
    claim = ("The Northwind Health pipeline holds 6 programmes in clinical trials at the end "
             "of fiscal 2025.")
    v, judged, _ = check(make_deps, claim, cited=2)
    assert v.verdict == "verified" and judged[0].startswith(f"Statement: {claim}")


def test_an_added_qualifier_is_still_judged_and_rejected(make_deps):
    wider = ("Northwind Health cardiology research and development spending was 71 million "
             "dollars in fiscal 2025.")
    v, judged, _ = check(make_deps, wider)
    assert v.verdict == "unverified" and judged[0].startswith(f"Statement: {wider}")


def test_identity_alone_never_verifies(make_deps):
    never = ScriptedChat(reply=lambda m: AIMessage(content=json.dumps({"verdicts": [
        {"passage": 1, "verdict": "INSUFFICIENT_EVIDENCE"}]})))
    v, _, _ = check(make_deps, RND, model=never)
    assert v.verdict == "unverified"


def test_a_passage_about_another_company_is_not_given_to_the_document(make_deps):
    chunks = list(CHUNKS)
    chunks[1] = "Medtronic research and development spending was 71 million dollars in fiscal 2025."
    v, judged, _ = check(make_deps, RND, chunks=chunks)
    assert v.verdict == "unverified"
    assert judged and all(j.startswith(f"Statement: {RND}") for j in judged)  # judged as written


def test_web_claims_get_no_identity_context(make_deps):
    v, judged, _ = check(make_deps, RND, kind="web")
    assert v.verdict == "unverified"
    assert judged and all(j.startswith(f"Statement: {RND}") and "Identity context" not in j
                          for j in judged)


def test_historical_claims_of_an_uploaded_document_get_its_identity_too(make_deps):
    """Identity is a property of the cited document, not of the Claim's kind: T18 CS2 kept
    Historical Claims out only because its credit removed words from the Claim, and nothing is
    removed now."""
    v, judged, _ = check(make_deps, RND, historical=True)
    assert v.check == "historical_document_passages" and v.verdict == "verified"
    assert judged and all(IDENTITY in j for j in judged)


def test_a_claim_whose_passage_names_the_company_is_judged_as_written(make_deps):
    v, judged, _ = check(make_deps, CHUNKS[0], cited=0)
    assert v.verdict == "verified" and judged[0].startswith(f"Statement: {CHUNKS[0]}")
