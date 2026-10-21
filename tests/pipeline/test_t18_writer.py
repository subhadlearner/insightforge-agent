"""T18 CS4: what the Writer is told about time, comparisons and the research's own gaps.

- Each Evidence line shows its period, or that none was stated with the Source's publication
  date, and whether it may predate the window the Sub-task asked for; never "outdated", and a
  publication date is never shown as the period of the fact.
- The Evidence budget counts exactly the text the Writer is shown.
- Two approved prompt rules: absence is stated as "this research did not establish", and
  figures are compared only when comparable. A summary sentence asserting absence is logged,
  never removed.
- Gap lines are written by code from the Run's records, in fixed wording that never calls
  retrieved material verified and never claims a Source lacks something."""

import json
from datetime import UTC, date, datetime

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    PassageRef,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.pipeline.synthesize import bundle_tokens
from insightforge_agent.pipeline.write import (
    SYSTEM,
    WRITER_PROMPT_VERSION,
    absence_sentences,
    render_bundle,
    research_gaps,
    write_report,
)
from tests.scripted import ScriptedChat

REF = PassageRef(observation_id="o", index=0)


def item(id_="ev_1", period="UNKNOWN", published=None, ambiguous=None, before=False,
         derived=None, statement="BYD cut prices by 34%.", subtask="s1"):
    return EvidenceItem(id=id_, subtask_id=subtask, aspect_id="a1", statement=statement,
                        confidence="LOW", supporting=[REF], primary=REF, as_of_period=period,
                        published_at=published, before_window=before, derived_from=derived,
                        temporally_ambiguous=(period == "UNKNOWN") if ambiguous is None
                        else ambiguous)


def bundle(*items):
    return EvidenceBundle(sections=[EvidenceSection(aspect_id="a1", heading="Pricing",
                                                    confidence="LOW", items=list(items))])


# ---- what each Evidence line says about time --------------------------------------------------

@pytest.mark.parametrize("it, shown", [
    (item(period="2025-Q3"), "- [ev_1] (LOW; as of 2025-Q3) BYD cut prices by 34%."),
    (item(published=date(2025, 10, 9)),
     "- [ev_1] (LOW; period not stated; published 2025-10) BYD cut prices by 34%."),
    (item(), "- [ev_1] (LOW; period not stated; undated) BYD cut prices by 34%."),
    (item(published=date(2025, 5, 27), before=True),
     "- [ev_1] (LOW; period not stated; published 2025-05; may predate the requested window) "
     "BYD cut prices by 34%."),
    (item(period="2026-10-19", derived="observation_date", ambiguous=False),
     "- [ev_1] (LOW; as of 2026-10-19, the date it was read; undated source) BYD cut prices by 34%."),
], ids=["stated period", "published", "undated", "may predate the window", "derived"])
def test_each_line_shows_its_period_or_publication_date_never_one_as_the_other(it, shown):
    assert render_bundle(bundle(it)).splitlines()[1] == shown


def test_a_stated_period_is_never_flagged_whatever_the_publication_date():
    line = render_bundle(bundle(item(period="2024", published=date(2020, 1, 1)))).splitlines()[1]
    assert "predate" not in line and "outdated" not in line and "published" not in line


# ---- synthesis records what the line needs ----------------------------------------------------

def test_synthesis_sets_publication_date_and_window_flag_conservatively(make_deps):
    from tests.pipeline.test_extraction_synthesis import BRIEF, NOW, OWNER, RUN, all_supported, light
    from insightforge_agent.pipeline.extract import extract
    from insightforge_agent.pipeline.synthesize import synthesize

    deps = make_deps(now=lambda: NOW)
    sources = {}
    for url, body, published in (
            ("https://old.test/a", "BYD cut prices by 34% to stimulate demand.",
             datetime(2025, 5, 27, tzinfo=UTC)),
            ("https://new.test/b", "BYD cut prices by 12% to clear stock.",
             datetime(2026, 7, 1, tzinfo=UTC)),
            ("https://dated.test/c", "BYD cut prices by 5% in 2024.",
             datetime(2024, 3, 1, tzinfo=UTC))):
        sources[url] = deps.store.ingest_web(owner_id=OWNER, run_id=RUN, url=url, title=url,
                                             body=body, fetched_at=NOW, credibility_score=0.7,
                                             published_at=published).source_id
    deps.light_model = light(draft=lambda ps, u: [
        {"statement": t, "passages": [n], "as_of_period": "2024" if "2024" in t else "UNKNOWN"}
        for n, t in ps], entail=all_supported)
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Pricing")], sub_tasks=[
        SubTask(id="s1", aspect_id="a1", query="BYD price cuts last six months",
                source_type="web", time_horizon_months=6, priority=1)])
    research = ResearchResults(results=[SubTaskResult(subtask_id="s1", status="succeeded",
        source_refs=[SourceRef(source_id=s, title=u, summary="s") for u, s in sources.items()])])
    scope = research.passage_scope()
    extraction = extract(deps, OWNER, RUN, list(scope), scope, plan=plan, research=research)
    b = synthesize(deps, OWNER, RUN, BRIEF, plan, research, extraction)
    by_text = {i.statement: i for s in b.sections for i in s.items}
    old, new, dated = (by_text[t] for t in (
        "BYD cut prices by 34% to stimulate demand.", "BYD cut prices by 12% to clear stock.",
        "BYD cut prices by 5% in 2024."))
    assert (old.published_at, old.before_window) == (date(2025, 5, 27), True)
    assert (new.published_at, new.before_window) == (date(2026, 7, 1), False)
    assert dated.as_of_period == "2024" and dated.before_window is False  # a stated period


# ---- the budget counts what the Writer sees ---------------------------------------------------

def test_the_budget_counts_exactly_the_bundle_text_the_writer_is_shown(make_deps):
    seen = []

    def reply(messages):
        seen.append(str(messages[-1].content))
        return AIMessage(content=json.dumps({"title": "T", "summary": "BYD cut prices by 34%.",
            "sections": [{"heading": "Pricing", "claims": [
                {"text": "BYD cut prices by 34%.", "evidence_id": "ev_1"}]}]}))

    deps = make_deps(writer_model=ScriptedChat(reply=reply))
    b = bundle(item(published=date(2025, 5, 27), before=True))
    write_report(deps, "alice", "run_1", "BYD pricing", b, [])
    (payload,) = seen
    assert render_bundle(b) in payload
    assert bundle_tokens(b) == estimate_tokens(render_bundle(b))


def test_more_evidence_than_the_budget_is_cut_lowest_rank_first_and_logged(make_deps):
    from tests.pipeline.test_extraction_synthesis import BRIEF, NOW, OWNER, RUN, all_supported, light
    from insightforge_agent.pipeline.extract import extract
    from insightforge_agent.pipeline.synthesize import synthesize

    deps = make_deps(now=lambda: NOW, evidence_budget_tokens=400)
    body = "\n\n".join(f"BYD cut the price of model {n} by {n + 10}% in 2026." for n in range(60))
    sid = deps.store.ingest_web(owner_id=OWNER, run_id=RUN, url="https://a.test/x", title="x",
                                body=body, fetched_at=NOW, credibility_score=0.7).source_id
    deps.light_model = light(draft=lambda ps, u: [
        {"statement": t, "passages": [n], "as_of_period": "2026"} for n, t in ps],
        entail=all_supported)
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="Pricing")], sub_tasks=[
        SubTask(id="s1", aspect_id="a1", query="q", source_type="web", time_horizon_months=12,
                priority=1)])
    research = ResearchResults(results=[SubTaskResult(subtask_id="s1", status="succeeded",
        source_refs=[SourceRef(source_id=sid, title="x", summary="s")])])
    scope = research.passage_scope()
    extraction = extract(deps, OWNER, RUN, list(scope), scope, plan=plan, research=research)
    b = synthesize(deps, OWNER, RUN, BRIEF, plan, research, extraction)
    cut = [e.payload for e in deps.repos.events.read_after(OWNER, RUN) if e.type == "evidence_cut"]
    kept = sum(len(s.items) for s in b.sections)
    assert bundle_tokens(b) <= 400 and kept + len(cut) == 60 and cut
    assert sorted(c["rank"] for c in cut) == list(range(kept + 1, 61))  # the lowest ranks go


# ---- the approved prompt rules ----------------------------------------------------------------

def test_the_writer_is_told_how_to_state_absence_and_when_to_compare():
    prompt = " ".join(SYSTEM.split())
    assert WRITER_PROMPT_VERSION == "writer-4"
    assert "say that this research did not establish it" in prompt
    assert "never state that a source, document or company lacks" in prompt
    assert "same thing, in the same unit, for the same period and scope" in prompt


def test_the_research_gaps_are_shown_to_the_writer_and_attached_as_before(make_deps):
    seen = []

    def reply(messages):
        seen.append(str(messages[-1].content))
        return AIMessage(content=json.dumps({"title": "T", "summary": "BYD cut prices by 34%.",
            "sections": [{"heading": "Pricing", "claims": [
                {"text": "BYD cut prices by 34%.", "evidence_id": "ev_1"}]}]}))

    deps = make_deps(writer_model=ScriptedChat(reply=reply))
    gaps = ["Sub-task s2 (R&D): this research did not establish it."]
    draft = write_report(deps, "alice", "run_1", "Brief", bundle(item()), gaps)
    assert "Research gaps (stated by the pipeline):\n- Sub-task s2 (R&D)" in seen[0]
    assert draft.gaps == gaps
    write_report(deps, "alice", "run_1", "Brief", bundle(item()), [])
    assert "Research gaps" not in seen[1]


@pytest.mark.parametrize("sentence, flagged", [
    ("The evidence contains no research and development pipeline detail beyond that timing.", True),
    ("The only risk item in the evidence is the expected regulatory approval of NW-200.", True),
    ("The annual report does not disclose segment revenue.", True),
    ("This research did not establish R&D spending.", False),
    ("Only one supplier provides the titanium.", False),
    ("Revenue did not grow in 2024.", False),
])
def test_absence_assertions_about_sources_are_detected(sentence, flagged):
    assert bool(absence_sentences(sentence)) is flagged


def test_a_flagged_summary_sentence_is_logged_and_the_flag_removes_nothing(make_deps):
    """The T18 CS4 absence flag is a measurement of the Writer's summary: it never removes a
    sentence. Since the PR #77 review the support check does, when the published Claims do not
    support it (here no Claim says anything about R&D, and its "no" is quoted by none)."""
    summary = ("BYD cut prices by 34%. The evidence contains no research and development "
               "pipeline detail.")

    def reply(messages):
        return AIMessage(content=json.dumps({"title": "T", "summary": summary, "sections": [
            {"heading": "Pricing", "claims": [{"text": "BYD cut prices by 34%.",
                                               "evidence_id": "ev_1"}]}]}))

    deps = make_deps(writer_model=ScriptedChat(reply=reply))
    draft = write_report(deps, "alice", "run_1", "Brief", bundle(item()), [])
    events = deps.repos.events.read_after("alice", "run_1")
    (flag,) = [e.payload for e in events if e.type == "summary_absence_flagged"]
    assert flag["sentences"] == ["The evidence contains no research and development pipeline "
                                 "detail."]
    (checked,) = [e.payload for e in events if e.type == "summary_checked"]
    assert checked["removed"] == flag["sentences"]  # removed by the support check, not the flag
    assert draft.summary == "BYD cut prices by 34%."


# ---- gap lines written by code ----------------------------------------------------------------

PLAN = SubTaskPlan(aspects=[Aspect(id="a1", name="Revenue"), Aspect(id="a2", name="R&D"),
                            Aspect(id="a3", name="Risks"), Aspect(id="a4", name="Market")],
                   sub_tasks=[SubTask(id=f"s{n}", aspect_id=f"a{n}", query=q,
                                      source_type="documents" if n < 4 else "web",
                                      time_horizon_months=12, priority=n)
                              for n, q in enumerate(["revenue", "R&D pipeline", "risks",
                                                     "market growth"], start=1)])
DOC = [Passage(observation_id="d", index=i, text=t, page=p, section_heading=h)
       for i, (p, h, t) in enumerate([(1, "Letter", "Revenue rose."), (2, "Research", "R&D up."),
                                      (2, "Research", "6 programmes."), (3, "Risks", "Supplier.")])]


def ev(type_, **payload):
    return {"type": type_, "payload": payload}


def gaps_for(results, items, events):
    return research_gaps(plan=PLAN, research=ResearchResults(results=results),
                         bundle=EvidenceBundle(sections=[EvidenceSection(
                             aspect_id="a1", heading="h", confidence="LOW", items=items)]
                             if items else []),
                         events=events, documents=[("annual.pdf", DOC)])


def scope(sid, *indices, origin="doc_reader"):
    return SubTaskResult(subtask_id=sid, status="succeeded", scope_origin=origin, source_refs=[
        SourceRef(source_id="src", title="annual.pdf", summary="s",
                  passages=[PassageRef(observation_id="d", index=i) for i in indices])])


def test_gap_lines_are_factual_templates_by_category():
    results = [scope("s1", 0), scope("s2", 1, 2, origin="search_recovery"),
               SubTaskResult(subtask_id="s3", status="failed"), scope("s4")]
    items = [item("ev_r", subtask="s1"), item("ev_d", subtask="s2")]
    lines = gaps_for(results, items, [
        ev("subtask_failed", subtask_id="s2", reason="FAILED: only metadata, no text content"),
        ev("subtask_failed", subtask_id="s3", reason="FAILED: the file is not an annual report."),
        ev("document_recovery_outcome", subtask_id="s2", recovered_chunks=2,
           recovered_only_chunks=2, validated_items=1, retained_items=1),
        ev("evidence_dropped", subtask_id="s4", reason="no supported Passage"),
        ev("evidence_dropped", subtask_id="s4",
           reason="no Passage passes the provenance and literal-value checks"),
        ev("draft_batch_failed", subtask_id="s4")])
    assert lines == [
        "Sub-task s2 (R&D pipeline): the Document Reader failed (its search results showed no "
        "chunk text); 2 chunk(s) were recovered from its own searches as candidates, and 1 "
        "Evidence item(s) citing them were kept after the provenance, figure and entailment "
        "checks.",
        "Sub-task s3 (risks) failed: its researcher reported that it could not complete it (the "
        "reply is kept in the Run log).",
        "Sub-task s4 (market growth): this research did not establish it; 2 drafted item(s) were "
        "not kept (1 failed the provenance or figure checks, 1 not supported by the entailment "
        "check), and 1 drafting call(s) failed.",
        "annual.pdf: not used by this Run: Risks (p3).",
    ]
    text = " ".join(lines).lower()
    assert "verified" not in text and "confirmed" not in text and "does not contain" not in text


def test_a_recovery_that_kept_nothing_says_so_and_full_coverage_adds_no_line():
    results = [scope("s1", 0, 3), scope("s2", 1, 2, origin="search_recovery"), scope("s3"),
               scope("s4")]
    items = [item("ev_r", subtask="s1"), item("ev_x", subtask="s3"), item("ev_m", subtask="s4")]
    lines = gaps_for(results, items, [
        ev("document_recovery_outcome", subtask_id="s2", recovered_chunks=2,
           recovered_only_chunks=2, validated_items=0, retained_items=0)])
    assert lines == ["Sub-task s2 (R&D pipeline): the Document Reader failed (its search results "
                     "showed no chunk text); 2 chunk(s) were recovered from its own searches as "
                     "candidates; none was kept after the provenance, figure and entailment "
                     "checks."]


def test_the_researchers_own_words_never_reach_a_gap_line():
    """Model prose may claim a source lacks something; code never repeats it."""
    results = [scope("s1", 0, 1, 2, 3), scope("s2"), SubTaskResult(subtask_id="s3", status="failed"),
               scope("s4")]
    items = [item("ev_a", subtask="s1"), item("ev_b", subtask="s2"), item("ev_c", subtask="s4")]
    lines = gaps_for(results, items, [ev("subtask_failed", subtask_id="s3",
                                         reason="FAILED: the annual report does not disclose risks.")])
    assert lines == ["Sub-task s3 (risks) failed: its researcher reported that it could not "
                     "complete it (the reply is kept in the Run log)."]
    assert "disclose" not in lines[0]


def test_items_cut_by_the_budget_are_not_called_unestablished():
    results = [scope("s1", 0, 1, 2, 3), scope("s2"), scope("s3"), scope("s4")]
    items = [item("ev_a", subtask="s1"), item("ev_b", subtask="s2"), item("ev_c", subtask="s3")]
    lines = gaps_for(results, items, [ev("evidence_cut", subtask_id="s4", rank=9),
                                      ev("evidence_cut", subtask_id="s4", rank=10)])
    assert lines == ["Sub-task s4 (market growth): its 2 Evidence item(s) were cut by the evidence "
                     "budget, below higher-ranked Evidence."]
