"""T18 CS2 through the graph: a documents Sub-task whose Document Reader failed only because its
search shows no chunk text is recovered from that Sub-task's own searches, as candidates that
Synthesis still validates. Everything else keeps today's behaviour. Scripted models; the
document is the T7 test annual report."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.domain.models import RunState, SubTaskStatus
from insightforge_agent.stores.doc_chunks import ChunkHit
from insightforge_agent.pipeline.graph import run_brief
from tests.pipeline.test_t7_documents import (
    REPORT,
    brief_with,
    doc_deps,  # noqa: F401 - fixture
    events,
    pdf,
    planner,
    researchers,
    run_with_document,
)
from tests.scripted import SUPPORT_PREFIX, ScriptedChat, ai, call, support_verdicts

T18_REPLY = ("FAILED: doc_search returned only metadata (source file, page, and relevance score) "
             "for 8 chunks, with no text content. I can't confirm that any of them cover the "
             "pipeline, so I'm not listing them. They need to be read directly to verify.")


def reader(s2_reply: str, s2_searches: int = 2) -> ScriptedChat:
    """s1 behaves as before; s2's Document Reader searches `s2_searches` times (0: never),
    then replies `s2_reply`. Web Sub-tasks behave normally."""
    def reply(messages) -> AIMessage:
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        task = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        if "Document Reader" in system and task.startswith("SUBTASK_ID=s2 "):
            done = sum(isinstance(m, ToolMessage) for m in messages)
            if done < s2_searches:
                query = ["research pipeline clinical programmes", "phase 3 trials 2025"][done]
                return ai(call("doc_search", {"query": query}, f"s2-search-{done}"))
            return AIMessage(content=s2_reply)
        return researchers().reply(messages)
    return ScriptedChat(reply=reply)


def run(doc_deps, s2_reply=T18_REPLY, s2_searches=2):
    deps = doc_deps(researcher_model=reader(s2_reply, s2_searches))
    snapshots = []
    uploads = [pdf()]
    r = run_brief(deps, "alice", brief_with(uploads), uploads=uploads, on_state=snapshots.append)
    research = next(s["research"] for s in reversed(snapshots) if "research" in s)
    results = {x["subtask_id"]: x for x in research["results"]}
    return deps, r, results, snapshots


def searched(deps, r, sid):
    return [e.payload["chunk_ids"] for e in events(deps, r, "doc_search")
            if e.payload["subtask_id"] == sid]


def test_a_metadata_only_failure_is_recovered_from_its_own_searches(doc_deps):
    deps, r, results, snapshots = run(doc_deps)
    assert r.state == RunState.COMPLETE
    s2 = results["s2"]
    assert (s2["status"], s2["scope_origin"]) == ("succeeded", "search_recovery")
    recovered = [f"{p['observation_id']}:{p['index']}" for ref in s2["source_refs"]
                 for p in ref["passages"]]
    own = searched(deps, r, "s2")
    assert len(own) == 2 and set(recovered) == {c for ids in own for c in ids}
    assert "recovered from this Sub-task's own searches" in s2["source_refs"][0]["summary"]
    # The original failure, then the recovery, recorded separately and in that order.
    failed = [e for e in events(deps, r) if e.type in ("subtask_failed", "document_scope_recovered")]
    assert [e.type for e in failed] == ["subtask_failed", "document_scope_recovered"]
    assert failed[0].payload["reason"].startswith("FAILED: doc_search returned only metadata")
    assert (failed[0].payload["searches"], failed[0].payload["returned_chunks"]) == (
        2, len(set(recovered)))
    rec = failed[1].payload
    assert (rec["trigger"], rec["lexicon"]) == ("metadata_only_limitation",
                                                "DOC_READER_LIMITATION_V2")
    assert rec["recovered"] == recovered and rec["cap"] == 16
    # The user-visible Sub-task stays failed: the Document Reader did fail.
    status = {s.subtask.id: s.status for s in deps.repos.subtasks.list("alice", r.id)}
    assert status["s2"] == SubTaskStatus.FAILED


def test_recovered_chunks_are_candidates_and_their_outcome_is_counted(doc_deps):
    deps, r, results, _ = run(doc_deps)
    (outcome,) = [e.payload for e in events(deps, r, "document_recovery_outcome")]
    assert outcome["subtask_id"] == "s2" and outcome["recovered_chunks"] > 0
    # Validated (provenance, figures, entailment) and retained in the bundle are counted apart.
    assert 0 <= outcome["retained_items"] <= outcome["validated_items"]
    assert set(outcome) >= {"recovered_only_chunks", "validated_items", "retained_items"}


@pytest.mark.parametrize("reply, searches", [
    ("FAILED: none of the chunks are relevant to the research pipeline.", 2),
    ("FAILED: the uploaded file is not an annual report.", 2),
    (T18_REPLY, 0),  # it never searched: nothing of its own to recover
    ("FAILED: doc_search raised an error: connection timed out.", 2),
    ("FAILED: doc_search returned only metadata, and the chunks are not relevant to R&D.", 2),
], ids=["relevance judgment", "other reason", "no search", "tool error",
        "metadata-only but irrelevant"])
def test_any_other_failure_stays_failed_as_before(doc_deps, reply, searches):
    deps, r, results, _ = run(doc_deps, reply, searches)
    assert (results["s2"]["status"], results["s2"]["source_refs"]) == ("failed", [])
    assert events(deps, r, "document_scope_recovered") == []
    (failed,) = [e.payload for e in events(deps, r, "subtask_failed")]
    assert failed["subtask_id"] == "s2" and failed["reason"].startswith("FAILED:")


def test_recovery_never_takes_another_subtasks_chunks(doc_deps):
    deps, r, results, _ = run(doc_deps)
    own = {c for ids in searched(deps, r, "s2") for c in ids}
    only_s1 = {c for ids in searched(deps, r, "s1") for c in ids} - own
    recovered = {f"{p['observation_id']}:{p['index']}" for ref in results["s2"]["source_refs"]
                 for p in ref["passages"]}
    assert recovered <= own and recovered.isdisjoint(only_s1)


def test_recovery_never_takes_another_runs_upload_even_if_a_search_names_it(doc_deps):
    """An earlier Run's chunk named in this Run's search event is dropped by the trust checks."""
    deps = doc_deps(researcher_model=reader(T18_REPLY))
    earlier = run_with_document(deps)
    foreign = next(e.payload["chunk_ids"][0] for e in events(deps, earlier, "doc_search"))
    real_search = deps.doc_index.search

    def leaky(owner_id, run_id, query, k=8):
        obs, idx = foreign.split(":")
        return real_search(owner_id, run_id, query, k) + [ChunkHit(
            passage=PassageRef(observation_id=obs, index=int(idx)), source_id="src_other",
            source_filename="other.pdf", page=1, relevance_score=0.1)]

    deps.doc_index.search = leaky
    deps.planner_model = planner()  # the scripted Planner plays one Run
    uploads = [pdf("orrin-2.pdf", b"second")]
    snapshots = []
    r = run_brief(deps, "alice", brief_with(uploads), uploads=uploads, on_state=snapshots.append)
    research = next(s["research"] for s in reversed(snapshots) if "research" in s)
    s2 = next(x for x in research["results"] if x["subtask_id"] == "s2")
    recovered = {f"{p['observation_id']}:{p['index']}" for ref in s2["source_refs"]
                 for p in ref["passages"]}
    assert recovered and foreign not in recovered  # its own Run's chunks only
    (rec,) = [e.payload for e in events(deps, r, "document_scope_recovered")]
    assert foreign in rec["excluded_by_trust_checks"]


def test_another_users_run_never_shares_a_search_or_a_recovery(doc_deps):
    deps, r, results, _ = run(doc_deps)
    deps.planner_model = planner()  # the scripted Planner plays one Run
    other = run_with_document(deps, owner="bob")
    assert other.state == RunState.COMPLETE
    bob = events(deps, other, "document_scope_recovered")
    assert len(bob) == 1 and all(e.owner_id == "bob" for e in events(deps, other))
    alice = {c for e in events(deps, r, "doc_search") for c in e.payload["chunk_ids"]}
    assert alice.isdisjoint(bob[0].payload["recovered"])  # Bob's own upload's chunks only


# ---- the T18 Northwind chain, end to end (CS1B + CS2 together) -------------------------------

NORTHWIND = [  # the T18 test annual report's 11 chunks (pdf_chunks_out.txt)
    (1, "Letter to shareholders", "Northwind Health revenue reached 612 million dollars in "
     "fiscal 2025. Revenue grew 14 percent compared with fiscal 2024, led by the cardiology "
     "segment. Operating margin improved to 11.5 percent in fiscal 2025."),
    (1, "Segment results", "Cardiology devices contributed 341 million dollars of revenue in "
     "fiscal 2025. Orthopaedic implants contributed 187 million dollars of revenue in fiscal 2025."),
    (1, "Segment results", "Diagnostics contributed 84 million dollars of revenue in fiscal 2025."),
    (2, "Research and development", "Research and development spending was 71 million dollars "
     "in fiscal 2025."),
    (2, "Research and development", "The pipeline holds 6 programmes in clinical trials at the "
     "end of fiscal 2025."),
    (2, "Research and development", "The NW-200 heart valve completed its pivotal trial in "
     "September 2025."),
    (2, "Research and development", "Northwind expects regulatory approval for NW-200 in the "
     "first half of 2026."),
    (2, "Operations", "Northwind Health employed 2,140 people at the end of fiscal 2025."),
    (2, "Operations", "A second manufacturing site in Cork, Ireland, began production in 2025."),
    (3, "Principal risks", "A single supplier provides the titanium used in orthopaedic implants. "
     "Pricing pressure from hospital purchasing groups reduced average selling prices. Delays to "
     "the NW-200 approval would defer expected cardiology revenue."),
    (3, "Outlook", "Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent."),
]
NW_QUERIES = {"s1": "revenue segment performance cardiology orthopaedic diagnostics",
              "s2": "research and development pipeline programmes pivotal trial spending",
              "s3": "principal risks supplier pricing pressure approval delays"}


def northwind_world(doc_deps):
    from insightforge_agent.stores.source_store import PassageDraft

    class NorthwindChunker:
        def chunk(self, filename, data):
            return [PassageDraft(t, page=p, section_heading=h) for p, h, t in NORTHWIND]

    plan = {"plan": {
        "aspects": [{"id": f"a{n}", "name": name} for n, name in
                    ((1, "Segments"), (2, "R&D pipeline"), (3, "Principal risks"))],
        "sub_tasks": [{"id": sid, "aspect_id": f"a{n}", "query": q, "source_type": "documents",
                       "time_horizon_months": 12, "priority": n}
                      for n, (sid, q) in enumerate(NW_QUERIES.items(), start=1)]}}
    tasks = [call("task", {"description": f"SUBTASK_ID={sid} {q}", "subagent_type": "doc_reader"},
                  f"research-{sid}") for sid, q in NW_QUERIES.items()]
    planner_ = ScriptedChat(script=[ai(call("submit_plan", plan, "plan-1")), ai(text="Planned."),
                                    ai(*tasks), ai(text="Dispatched.")])

    def reader_reply(messages):
        system = " ".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        task = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        last = messages[-1]
        sid = task.split()[0].split("=")[1]
        searches = sum(isinstance(m, ToolMessage) for m in messages)
        if "Document Reader" not in system:
            return researchers().reply(messages)
        if sid == "s2" and searches < 2:
            return ai(call("doc_search", {"query": NW_QUERIES["s2"].split()[searches * 3]
                                          + " " + NW_QUERIES["s2"]}, f"s2-{searches}"))
        if sid == "s2":
            return AIMessage(content=T18_REPLY)
        if isinstance(last, ToolMessage):
            ids = [h["chunk_id"] for h in json.loads(str(last.content))][:5]  # as in T18
            return AIMessage(content=f"RESULT: chunks | chunks: {', '.join(ids)}")
        return ai(call("doc_search", {"query": NW_QUERIES[sid]}, f"{sid}-0"))

    def light_reply(messages):
        """Proposes facts as T18's light model drafted them (each sentence, the supplier risk
        restated with the company as subject), and answers the support judgment permissively."""
        system, user = str(messages[0].content), str(messages[-1].content)
        import re as _re
        numbered = _re.findall(r"^\[(\d+)\] (.+)$", user, _re.MULTILINE)
        if system.startswith("You extract facts"):
            facts = []
            for n, text in numbered:
                for sentence in _re.split(r"(?<=\.) (?=[A-Z])", text):
                    statement = (f"Northwind Health discloses that {sentence[0].lower()}"
                                 f"{sentence[1:]}" if "supplier" in sentence else sentence)
                    facts.append({"passage_index": int(n), "statement": statement,
                                  "entity": "Northwind Health", "entities": ["Northwind Health"]})
            return AIMessage(content=json.dumps({"facts": facts}))
        if system.startswith(SUPPORT_PREFIX):
            return AIMessage(content=json.dumps({"verdicts": support_verdicts(user)}))
        return AIMessage(content="A summary.")

    return doc_deps(planner_model=planner_, researcher_model=ScriptedChat(reply=reader_reply),
                    light_model=ScriptedChat(reply=light_reply), chunker=NorthwindChunker())


def test_the_t18_northwind_chain_now_keeps_segments_rnd_and_risks(doc_deps):
    deps = northwind_world(doc_deps)
    uploads = [pdf("northwind-health-annual-report-2025.pdf")]
    snapshots = []
    r = run_brief(deps, "alice", brief_with(uploads, web=False), uploads=uploads,
                  on_state=snapshots.append)
    assert r.state == RunState.COMPLETE
    bundle = next(s["bundle"] for s in reversed(snapshots) if "bundle" in s)
    statements = " | ".join(i["statement"] for s in bundle["sections"] for i in s["items"])
    for fact in ("341 million", "187 million", "84 million",          # segments (CS1B label)
                 "single supplier", "pricing pressure", "Delays to the NW-200" .lower()):  # risks
        assert fact.lower() in statements.lower(), fact
    (rec,) = [e.payload for e in events(deps, r, "document_scope_recovered")]
    recovered = {int(c.split(":")[1]) for c in rec["recovered"]}
    selected = {int(p["index"]) for x in next(s["research"] for s in reversed(snapshots)
                                              if "research" in s)["results"]
                if x["scope_origin"] == "doc_reader"
                for ref in x["source_refs"] for p in ref["passages"]}
    rnd = {3: "71 million", 4: "6 programmes", 5: "pivotal trial"}
    for chunk, fact in rnd.items():
        # An R&D fact is in the bundle exactly when some scope reached its chunk.
        assert (fact in statements) is (chunk in recovered | selected), fact
    only_recovered = [c for c in rnd if c in recovered - selected]
    assert only_recovered  # recovery, not the other Readers, brought some R&D back
    (outcome,) = [e.payload for e in events(deps, r, "document_recovery_outcome")]
    assert outcome["recovered_only_chunks"] == len(recovered - selected)
    assert outcome["retained_items"] >= len(only_recovered)
    # CS4: the Report's gaps state the recovery factually, and nothing claims an absence.
    report = deps.repos.reports.get_for_run("alice", r.id)
    gaps = report.body["draft"]["gaps"]
    assert any("recovered from its own searches as candidates" in g for g in gaps)
    assert not any("produced no usable sources" in g for g in gaps)
    assert not any("verified" in g.lower() for g in gaps)
