"""The cited Passage in the Fact-Checker (T18 CS1A, made the rule by ADR-0006 §7).

The cited Passage is always judged: on a web page through its exact re-fetched counterpart,
screened with no similarity floor, ahead of F2's own candidates, which it never evicts. It never
bypasses the figure gate or the support judgment. When the page does not hold it (changed,
ambiguous, stored elsewhere, or not looked up), the Passage as it was cited is judged for the
record (`cited_support`) and can never verify the Claim.

The embedder is a table (exact cosines) and the light model is scripted, recording every
support payload."""

import json
import math
import re

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.pipeline.fact_check import counterpart, fact_check
from insightforge_agent.domain.models import Passage
from tests.pipeline.test_t6_writer_fact_check import URL, Fetcher, draft_of, evidence
from tests.scripted import ScriptedChat

CITED = "Germany added 15 GW of new solar PV capacity in 2025, its highest ever."
CLAIM = "Germany added 15 GW of new solar PV capacity in 2025 according to the industry body."


class Table:
    """The Claim is [1, 0]; a listed text sits at the given cosine; anything else at 0.10."""

    def __init__(self, scores=None):
        self.scores = scores or {}

    def embed(self, texts):
        out = []
        for t in texts:
            if t == CLAIM:
                out.append([1.0, 0.0])
            else:
                c = self.scores.get(t, 0.10)
                out.append([c, math.sqrt(1 - c * c)])
        return out


def judge(verdict="SUPPORTED"):
    """Each shown span gets `verdict` (or the next of a list); a SUPPORTED one carries the
    permissive support map (statement as one part, the shown text as its excerpt)."""
    def reply(messages):
        user = str(messages[-1].content)
        statement = user.split("\n", 1)[0].removeprefix("Statement: ")
        spans = re.findall(r"^\[(\d+)\] (.+)$", user, re.MULTILINE)
        v = verdict if isinstance(verdict, list) else [verdict] * len(spans)
        return AIMessage(content=json.dumps({"verdicts": [
            {"passage": int(n), "verdict": v[i],
             **({"parts": [{"part": statement, "excerpts": [t]}]} if v[i] == "SUPPORTED" else {})}
            for i, (n, t) in enumerate(spans)]}))
    return ScriptedChat(reply=reply)


def run(make_deps, page, *, stored=(CITED,), claim=CLAIM, model=None, scores=None, flag=True,
        kind="web", store=None):
    model = model or judge()
    deps = make_deps(fetcher=Fetcher({URL: page}), light_model=model, embedder=Table(scores),
                     fact_check_counterpart=flag)
    _, bundle = evidence(deps, list(stored), kind=kind)
    if store is not None:  # only the re-fetch goes through it, never the cited Evidence
        deps.store = store(deps.store)
    (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
    sent = [str(c[-1].content) for c in model.seen]
    events = [e.payload for e in deps.repos.events.read_after("alice", "run_1")
              if e.type == "fact_check_counterpart"]
    return v, sent, events


def same_verdict(a, b):
    """Equal verdicts from two separate worlds: Observation ids are random per world, so the
    cited and matched Passages compare by index."""
    def view(v):
        return (v.verdict, v.check, v.similarity, v.reason, v.original.index,
                v.matched.index if v.matched else None)
    return view(a) == view(b)


# ---- the counterpart, by content -------------------------------------------------------------

def P(i, text):
    return Passage(observation_id="new", index=i, text=text)


def test_the_counterpart_is_the_one_refetched_passage_with_the_same_normalised_text():
    original = Passage(observation_id="old", index=2, text="Germany  added 15 GW.")
    assert counterpart(original, [P(0, "x"), P(5, "germany added 15 GW.")]) == (
        P(5, "germany added 15 GW."), "unique")
    assert counterpart(original, [P(0, "Germany added 16 GW.")]) == (None, "absent")
    # Equal twice: the one at the original index, else it is ambiguous.
    assert counterpart(original, [P(2, "Germany added 15 GW."), P(7, "Germany added 15 GW.")]) \
        == (P(2, "Germany added 15 GW."), "unique_by_index")
    assert counterpart(original, [P(3, "Germany added 15 GW."), P(7, "Germany added 15 GW.")]) \
        == (None, "ambiguous")


# ---- the cited Passage is always judged ----------------------------------------------------

def test_with_no_eligible_f2_candidate_the_cited_passage_is_still_judged(make_deps):
    v, sent, (event,) = run(make_deps, f"Intro text.\n\n{CITED}")
    assert v.verdict == "verified" and v.matched.index == 1
    assert len(sent) == 1 and CITED in sent[0]
    assert v.similarity == pytest.approx(0.10)  # the counterpart's own cosine, reported as is
    assert (event["match"], event["original"]["index"], event["counterpart"]["index"]) == (
        "unique", 0, 1)
    assert v.cited_support == "SUPPORTED"


@pytest.mark.parametrize("answer", ["INSUFFICIENT_EVIDENCE", "CONTRADICTED",
                                    ["SUPPORTED", "CONTRADICTED"]])
def test_the_cited_passage_still_needs_an_unambiguous_supported_judgment(make_deps, answer):
    # A one-sentence counterpart gives one span; the mixed case judges it twice in one reply.
    model = judge(answer) if not isinstance(answer, list) else ScriptedChat(
        reply=lambda m: AIMessage(content=json.dumps({"verdicts": [
            {"passage": 1, "verdict": "SUPPORTED", "parts": [{"part": CLAIM, "excerpts": [CITED]}]},
            {"passage": 1, "verdict": "CONTRADICTED"}]})))
    v, sent, (event,) = run(make_deps, CITED, model=model)
    assert v.verdict == "unverified" and len(sent) == 1
    assert "no candidate was judged SUPPORTED" in v.reason
    assert event["match"] == "unique"


def test_a_malformed_reply_leaves_it_unverified(make_deps):
    model = ScriptedChat(reply=lambda m: AIMessage(content="not json"))
    v, _, (event,) = run(make_deps, CITED, model=model)
    assert v.verdict == "unverified" and "VERIFICATION_ERROR" in v.reason


@pytest.mark.parametrize("page, match, why", [
    ("Germany added 16 GW of new solar PV capacity in 2025, its highest ever.", "absent",
     "no longer holds the cited Passage (page drift)"),
    (f"Other.\n\n{CITED}\n\n{CITED}", "ambiguous", "more than once, so it could not be matched"),
], ids=["page changed", "ambiguous"])
def test_when_the_page_does_not_hold_it_the_cited_passage_is_judged_for_the_record_only(
        make_deps, page, match, why):
    v, sent, (event,) = run(make_deps, page)
    assert event["match"] == match and len(sent) == 1 and CITED in sent[0]
    assert v.verdict == "unverified" and v.cited_support == "SUPPORTED"
    assert why in v.reason  # citation truth kept apart from the page's current state


def test_a_cited_passage_that_does_not_state_the_figures_makes_no_call(make_deps):
    claim_off = CLAIM.replace("15 GW", "51 GW")
    v, sent, (event,) = run(make_deps, CITED, claim=claim_off)
    assert sent == [] and v.verdict == "unverified" and "51" in v.reason
    assert event["match"] == "unique" and v.cited_support == "INSUFFICIENT_EVIDENCE"


def test_a_refetch_stored_under_another_source_is_never_a_counterpart(make_deps):
    class Elsewhere:
        def __init__(self, store):
            self._store = store

        def __getattr__(self, name):
            return getattr(self._store, name)

        def ingest_web(self, **kw):
            return self._store.ingest_web(**{**kw, "url": kw["url"] + "?moved"})

    v, sent, (event,) = run(make_deps, CITED, store=Elsewhere)
    assert event["match"] == "source_mismatch" and v.verdict == "unverified"
    assert len(sent) == 1 and "stored under another Source" in v.reason  # judged as cited only


def test_eligible_f2_candidates_are_all_judged_and_none_is_evicted(make_deps):
    a = "Germany added 15 GW of solar capacity in 2025, says the industry body."
    b = "In 2025 Germany installed 15 GW of solar PV capacity, the industry body said."
    c = "Germany's 15 GW of new solar capacity in 2025 was a record, said the industry body."
    page = "\n\n".join([a, b, c, CITED])
    scores = {a: 0.95, b: 0.93, c: 0.90}
    v, sent, (event,) = run(make_deps, page, scores=scores, model=judge("INSUFFICIENT_EVIDENCE"))
    shown = re.findall(r"^\[\d+\] (.+)$", sent[0], re.MULTILINE)
    assert len(sent) == 1 and shown == [CITED, a, b, c]  # the cited Passage, then F2's three
    assert event["match"] == "unique" and v.verdict == "unverified"


def test_a_document_claim_judges_its_stored_cited_chunk(make_deps):
    v, sent, events = run(make_deps, "unused", kind="pdf", model=judge("INSUFFICIENT_EVIDENCE"))
    assert v.check == "document_passages" and v.verdict == "unverified"
    assert len(sent) == 1 and CITED in sent[0] and events == []  # no re-fetch, no lookup
    assert v.cited_support == "INSUFFICIENT_EVIDENCE"


def test_with_the_setting_off_nothing_is_looked_up_and_the_cited_text_cannot_verify(make_deps):
    v, sent, events = run(make_deps, CITED, flag=False)
    assert events == [] and v.verdict == "unverified"
    assert len(sent) == 1 and v.cited_support == "SUPPORTED"  # judged as cited, for the record


# ---- inherited (T8) Historical Claims follow the same fallback -------------------------------

def test_a_historical_web_claim_uses_the_counterpart_of_its_original_passage(make_deps):
    from datetime import date

    from insightforge_agent.domain.contracts import (
        EvidenceBundle,
        HistoricalContext,
        HistoricalItem,
        PassageRef,
    )
    from insightforge_agent.domain.memory import without_report_date

    day = date(2026, 9, 1)
    dated = CLAIM[:-1] + " (reported 2026-09-01)."
    stripped = without_report_date(dated, day)

    class HistTable(Table):
        def embed(self, texts):
            return [[1.0, 0.0] if t == stripped else v
                    for t, v in zip(texts, super().embed(texts), strict=True)]

    model = judge()
    deps = make_deps(fetcher=Fetcher({URL: f"Intro.\n\n{CITED}"}), light_model=model,
                     embedder=HistTable())
    obs, _ = evidence(deps, [CITED])  # the earlier Run's Observation
    original = PassageRef(observation_id=obs.id, index=0)
    hist = HistoricalContext(items=[HistoricalItem(
        id="hist_1", subtask_id="m1", aspect_id="a1", heading="Sales", statement=CITED,
        entity="Germany", report_id="rep_old", run_id="run_old", report_date=day,
        as_of_period="2025", primary=original, supporting=[original])])
    (v,) = fact_check(deps, "alice", "run_new", draft_of([dated], ["hist_1"], historical=(0,)),
                      EvidenceBundle(sections=[]), hist).verdicts
    assert (v.verdict, v.check, v.original) == ("verified", "historical_web_refetch", original)
    assert v.matched.observation_id != obs.id and v.matched.index == 1  # the new fetch


def test_a_cited_passage_that_is_gone_leaves_the_f2_verdict(make_deps):
    from insightforge_agent.domain.errors import NotFoundError

    class Forgetful:
        def __init__(self, store):
            self._store = store

        def __getattr__(self, name):
            return getattr(self._store, name)

        def get_passages(self, owner_id, observation_id, indices):
            raise NotFoundError("passage removed")

    on, sent, (event,) = run(make_deps, CITED, store=Forgetful)
    off, _, _ = run(make_deps, CITED, flag=False, store=Forgetful)
    assert same_verdict(on, off) and sent == []
    assert (event["match"], on.cited_support) == ("cited_missing", None)
