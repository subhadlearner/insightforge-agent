"""ADR-0006 §5: the requirements ledger's rules, decided in code from the Run's own records."""

from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceItem,
    ExtractionResult,
    FailedUnit,
    PassageRef,
    ReportDraft,
    ReportSection,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.evidence import compress_to_budget
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.domain.requirements import (
    Requirement,
    RequirementLedger,
    aspect_ledger,
    assess,
    coverage_index,
    gap_lines,
    ledger_problems,
    links,
    mark_reported,
)

PLAN = SubTaskPlan(
    aspects=[Aspect(id="a1", name="Revenue"), Aspect(id="a2", name="Risks"),
             Aspect(id="a3", name="Market")],
    sub_tasks=[SubTask(id=f"s{n}", aspect_id=f"a{n}", query=f"q{n}", source_type="web",
                       time_horizon_months=12, priority=n) for n in (1, 2, 3)])
LEDGER = RequirementLedger(origin="brief", requirements=[
    Requirement(id="R1", question="Revenue in 2025?", aspect_ids=["a1"]),
    Requirement(id="R2", question="Segment revenue?", aspect_ids=["a1"]),
    Requirement(id="R3", question="Principal risks?", aspect_ids=["a2"]),
    Requirement(id="R4", question="Market growth?", aspect_ids=["a3"], priority="optional")])
REF = PassageRef(observation_id="o", index=0)


def item(iid, aspect="a1", reqs=(), conflicting=False):
    return EvidenceItem(id=iid, subtask_id="s1", aspect_id=aspect, statement=f"{iid} statement.",
                        confidence="LOW", supporting=[REF], primary=REF,
                        requirement_ids=list(reqs), conflicting=conflicting)


def research(failed=(), sources=None):
    return ResearchResults(results=[
        SubTaskResult(subtask_id=t.id, status="failed" if t.id in failed else "succeeded",
                      source_refs=[SourceRef(source_id=s, title="t", summary="s")
                                   for s in (sources or {}).get(t.id, [])])
        for t in PLAN.sub_tasks])


def test_a_proposed_ledger_must_name_real_aspects_cover_them_all_and_have_unique_ids():
    assert ledger_problems(LEDGER, PLAN) == []
    bad = RequirementLedger(origin="brief", requirements=[
        Requirement(id="R1", question="x", aspect_ids=["a9"]),
        Requirement(id="R1", question=" ", aspect_ids=["a1"])])
    problems = " | ".join(ledger_problems(bad, PLAN))
    assert "not unique" in problems and "a9" in problems and "no question" in problems
    assert "no requirement covers Aspects ['a2', 'a3']" in problems


def test_the_fallback_is_one_required_requirement_per_aspect():
    ledger = aspect_ledger(PLAN)
    assert ledger.origin == "aspects" and ledger_problems(ledger, PLAN) == []
    assert [(r.id, r.question, r.aspect_ids) for r in ledger.requirements] == [
        ("R1", "Revenue", ["a1"]), ("R2", "Risks", ["a2"]), ("R3", "Market", ["a3"])]


def test_only_explicit_links_count_unless_the_ledger_is_the_aspect_fallback():
    linked = item("ev_1", reqs=["R2", "R9"])  # R9 is unknown: ignored
    assert links(linked, LEDGER) == ["R2"]
    assert links(item("ev_2"), LEDGER) == []  # its Aspect alone answers nothing
    assert links(item("ev_2"), aspect_ledger(PLAN)) == ["R1"]


def test_statuses_are_decided_from_kept_items_failures_and_lost_work():
    lost = ExtractionResult(failed_units=[FailedUnit(stage="extraction", source_id="src_m",
                                                     passages=[REF, REF], reason="truncated")])
    statuses = {s.id: s for s in assess(
        LEDGER, PLAN, research(failed={"s2"}, sources={"s3": ["src_m"]}), lost,
        [item("ev_1", reqs=["R1"]), item("ev_2", reqs=["R2"], conflicting=True)])}
    assert statuses["R1"].status == "supported" and statuses["R1"].evidence_ids == ["ev_1"]
    assert statuses["R2"].status == "contradictory"
    assert (statuses["R3"].status, statuses["R3"].reasons) == ("incomplete",
                                                               ["Sub-task(s) s2 failed"])
    assert statuses["R4"].status == "incomplete" and "could not be read" in statuses["R4"].reasons[0]


def test_nothing_failed_and_nothing_found_is_no_evidence():
    (r1, *_) = assess(LEDGER, PLAN, research(), ExtractionResult(), [])
    assert r1.status == "no_evidence"


def test_reported_needs_a_claim_citing_one_of_its_items_not_just_its_aspect():
    statuses = assess(LEDGER, PLAN, research(), ExtractionResult(),
                      [item("ev_1", reqs=["R1"]), item("ev_2", reqs=["R2"])])
    draft = ReportDraft(title="T", sections=[ReportSection(heading="Revenue", claims=[
        Claim(text="x", evidence_id="ev_9", premises=["ev_1"])])])  # R1 via a premise
    after = {s.id: s for s in mark_reported(statuses, draft)}
    assert (after["R1"].status, after["R1"].claims) == ("reported", ["0.0"])
    assert after["R2"].status == "supported"  # same Aspect, no Claim citing its item


def test_gap_lines_match_the_ledger_and_never_claim_an_absence_in_a_source():
    statuses = assess(LEDGER, PLAN, research(failed={"s2"}), ExtractionResult(),
                      [item("ev_1", reqs=["R1"])])
    lines = gap_lines(statuses)
    assert [line.split(" (")[0] for line in lines] == [
        "Requirement R2", "Requirement R3", "Requirement R4"]
    assert lines[1] == ("Requirement R3 (Principal risks?): this research did not establish it; "
                        "the research for it did not complete (Sub-task(s) s2 failed).")
    for line in lines:
        assert "verified" not in line.lower()
        assert not any(w in line.lower() for w in ("does not disclose", "lacks", "no data"))


def test_the_writer_index_says_what_is_answered_and_what_is_not():
    statuses = assess(LEDGER, PLAN, research(), ExtractionResult(), [item("ev_1", reqs=["R1"])])
    index = coverage_index(statuses).splitlines()
    assert index[0] == "- R1 [required] Revenue in 2025?: answered by ev_1"
    assert index[3].startswith("- R4 [optional] Market growth?: NOT established")


def test_budget_compression_keeps_the_last_support_of_a_required_requirement():
    ranked = [item("ev_a", reqs=["R1"]), item("ev_b", reqs=["R1"]), item("ev_c", reqs=["R2"])]

    def essential(i, kept):
        return any(r in ("R1", "R2") and not any(r in o.requirement_ids for o in kept if o is not i)
                   for r in i.requirement_ids)

    kept, cut = compress_to_budget(ranked, lambda chosen: len(chosen) <= 2, essential)
    assert [i.id for i in kept] == ["ev_a", "ev_c"] and [i.id for i in cut] == ["ev_b"]
    kept, cut = compress_to_budget(ranked, lambda chosen: len(chosen) <= 1, essential)
    assert [i.id for i in cut] == ["ev_b", "ev_c"]  # only when nothing else can go
