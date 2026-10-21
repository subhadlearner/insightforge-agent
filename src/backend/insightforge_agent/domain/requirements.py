"""The requirements ledger (ADR-0006 §5): what the Brief asks, and whether this Run answered it.

A requirement is one atomic question of the Brief, linked to the Aspects that research it.
Its status is decided in code from the Run's own records, never from similarity:

- supported: a kept Evidence item linked to it (explicitly, or through its Aspect when the
  ledger fell back to one requirement per Aspect) was verified by the support contract and,
  when the answer check ran (`AnswerCheck`), was judged to answer its question: a link is
  model output, so a valid but wrong requirement id never answers it (PR #77 review);
- partial: verified, answering items state some of the parts of its question (one per
  company, measure, period...) but not all; the gap line names the parts not established;
- contradictory: its only verified, answering items conflict;
- incomplete: nothing verified, and a Sub-task researching it failed or a work unit covering
  its Sub-tasks' material could not be read after bounded recovery;
- budget_omitted: nothing kept, but verified Evidence linked to it was left out to fit the
  Evidence budget (final PR #77 review: never reported as "no evidence");
- verification_incomplete: nothing verified, nothing failed to run, but a check that could
  have established it failed: a candidate's support judgment, or the answer check itself
  (PR #77 review: never reported as "no evidence");
- no_evidence: nothing verified, and nothing failed;
- background: only Historical items from earlier Reports bear on it (memory Sub-tasks);
- reported (after writing): a Claim of the Report cites one of its items;
- reported_unverified (after fact-checking): every Claim answering it was sampled and judged
  unverified by the Fact-Checker, page drift aside (`after_fact_check`).

Everything not supported or reported becomes a fixed-wording gap line, so the Report's
"Gaps & limitations" always matches the ledger. A requirement is never "reported" because its
Aspect has some Claim: a Claim must cite an item linked to that requirement.

Whether the ledger captures everything the Brief asks is a semantic judgment, made by an
independent coverage check (`pipeline/requirements.check_coverage`), never by word matching
(final PR #77 review). What the Brief asks that no requirement covers is recorded on the ledger
(`uncovered_asks`, the Brief's own words) and stated in the Report as not established; a check
that failed is recorded too, and the Report says its coverage of the Brief is unknown
(`decomposition_gaps`)."""

from typing import Literal

from pydantic import BaseModel, Field

from insightforge_agent.domain.contracts import (
    ClaimVerdict,
    EvidenceItem,
    ExtractionResult,
    ReportDraft,
    ResearchResults,
)
from insightforge_agent.domain.evidence import DROP_UNVERIFIED
from insightforge_agent.domain.plan import SubTaskPlan

MAX_REQUIREMENTS = 12

Status = Literal["supported", "partial", "contradictory", "incomplete", "verification_incomplete",
                 "budget_omitted", "no_evidence", "background", "reported", "reported_unverified"]


class Requirement(BaseModel):
    id: str
    question: str
    priority: Literal["required", "optional"] = "required"
    aspect_ids: list[str] = Field(default_factory=list)


class RequirementLedger(BaseModel):
    version: int = 1
    origin: Literal["brief", "aspects"] = "aspects"  # "aspects": the fallback, one per Aspect
    requirements: list[Requirement] = Field(default_factory=list)
    # What the Brief asks that no requirement covers, in the Brief's words, after one retry;
    # and why the coverage check could not be made, when it failed (`check_coverage`).
    uncovered_asks: list[str] = Field(default_factory=list)
    coverage_failed: str = ""

    def ids(self) -> set[str]:
        return {r.id for r in self.requirements}


class RequirementStatus(BaseModel):
    id: str
    question: str
    priority: Literal["required", "optional"]
    status: Status
    evidence_ids: list[str] = Field(default_factory=list)  # kept, verified items linked to it
    claims: list[str] = Field(default_factory=list)  # "section.claim" of Claims citing them
    reasons: list[str] = Field(default_factory=list)
    missing_parts: list[str] = Field(default_factory=list)  # parts of the question not stated
    # After fact-checking: "section.claim" -> verified | unverified | page_drift | unchecked,
    # for the Claims above that were sampled.
    fact_check: dict[str, str] = Field(default_factory=dict)


class AnswerCheck(BaseModel):
    """Which kept items answer each requirement's question (PR #77 reviews): per requirement,
    the parts of its question (with any the coverage check found the split left out), the
    parts each linked item was verified to state (items stating none left out), the items
    stating any part, how many claimed answers the verifier was unsure of, and the
    requirements whose check failed."""

    answers: dict[str, list[str]] = Field(default_factory=dict)
    failed: list[str] = Field(default_factory=list)
    parts: dict[str, list[str]] = Field(default_factory=dict)
    states: dict[str, dict[str, list[int]]] = Field(default_factory=dict)
    unsure: dict[str, int] = Field(default_factory=dict)



def ledger_problems(ledger: RequirementLedger, plan: SubTaskPlan) -> list[str]:
    """Why a proposed ledger cannot be used as is."""
    aspects = {a.id for a in plan.aspects}
    out = []
    if not 1 <= len(ledger.requirements) <= MAX_REQUIREMENTS:
        out.append(f"{len(ledger.requirements)} requirements; 1 to {MAX_REQUIREMENTS} allowed")
    ids = [r.id for r in ledger.requirements]
    if len(set(ids)) != len(ids):
        out.append("requirement ids are not unique")
    for r in ledger.requirements:
        if not r.question.strip():
            out.append(f"{r.id} has no question")
        if not r.aspect_ids or set(r.aspect_ids) - aspects:
            out.append(f"{r.id} must name existing Aspects (has {r.aspect_ids})")
    uncovered = aspects - {a for r in ledger.requirements for a in r.aspect_ids}
    if uncovered:
        out.append(f"no requirement covers Aspects {sorted(uncovered)}")
    return out


def aspect_ledger(plan: SubTaskPlan) -> RequirementLedger:
    """The fallback: one required requirement per Aspect, asking for the Aspect itself."""
    return RequirementLedger(origin="aspects", requirements=[
        Requirement(id=f"R{n}", question=a.name, aspect_ids=[a.id])
        for n, a in enumerate(plan.aspects, start=1)])


def links(item: EvidenceItem, ledger: RequirementLedger) -> list[str]:
    """The requirements an item bears on: its explicit links, or, for the Aspect fallback,
    the requirement of its Aspect (there the requirement *is* the Aspect)."""
    if ledger.origin == "aspects":
        return [r.id for r in ledger.requirements if item.aspect_id in r.aspect_ids]
    known = ledger.ids()
    return [r for r in item.requirement_ids if r in known]


def decomposition_gaps(ledger: RequirementLedger) -> list[str]:
    """The Report's lines for what the Brief asks that the ledger does not track: each ask the
    coverage check found no requirement covers, in the Brief's own words, as not established;
    or, when that check failed, that the ledger's coverage of the Brief is unknown."""
    out = [f'The Brief asks for "{ask}", but no research requirement this Run tracked asks for '
           "it, so this research did not establish it." for ask in ledger.uncovered_asks]
    if ledger.coverage_failed:
        out.append("Whether this Run's research requirements cover everything the Brief asks "
                   "could not be checked (the check failed); something the Brief asks may not "
                   "have been researched.")
    return out


def unverifiable(drops: list[dict]) -> list[tuple[str, list[str]]]:
    """From `evidence_dropped` payloads: the Aspect and requirement links of each candidate
    dropped because its support check failed (`DROP_UNVERIFIED`)."""
    return [(d.get("aspect_id", ""), list(d.get("requirement_ids") or [])) for d in drops
            if d.get("reason") == DROP_UNVERIFIED]


def budget_cut(cuts: list[dict]) -> list[list[str]]:
    """From `evidence_cut` payloads: the requirement links of each verified item left out to
    fit the Evidence budget."""
    return [list(c.get("requirement_ids") or []) for c in cuts]


def assess(ledger: RequirementLedger, plan: SubTaskPlan, research: ResearchResults,
           extraction: ExtractionResult, kept: list[EvidenceItem],
           background_aspects: set[str] | None = None, *,
           answers: AnswerCheck | None = None,
           unverifiable: list[tuple[str, list[str]]] = (),
           cut: list[list[str]] = ()) -> list[RequirementStatus]:
    """Each requirement's status before writing, from the kept (verified, in-budget) items
    that, when `answers` is given, were judged to answer it; `unverifiable` lists the
    candidates whose support check failed (`unverifiable()`), `cut` the verified items left out
    to fit the Evidence budget (`budget_cut()`)."""
    failed_subtasks = {r.subtask_id for r in research.results if r.status == "failed"}
    lost_sources = {u.source_id for u in extraction.failed_units}
    found_by: dict[str, set[str]] = {}  # aspect id -> Source ids its Sub-tasks found
    for t in plan.sub_tasks:
        found_by.setdefault(t.aspect_id, set())
    by_task = {t.id: t for t in plan.sub_tasks}
    for r in research.results:
        if r.status == "succeeded" and r.subtask_id in by_task:
            found_by[by_task[r.subtask_id].aspect_id].update(s.source_id for s in r.source_refs)
    background_aspects = background_aspects or set()
    out = []
    for req in ledger.requirements:
        linked = [i for i in kept if req.id in links(i, ledger)]
        tasks = [t for t in plan.sub_tasks if t.aspect_id in req.aspect_ids]
        reasons: list[str] = []
        check_failed = False
        if answers is None:
            items = linked
        else:
            confirmed = set(answers.answers.get(req.id, []))
            items = [i for i in linked if i.id in confirmed]
            check_failed = req.id in answers.failed and len(items) < len(linked)
            refused = len(linked) - len(items)
            if refused and not check_failed:
                reasons.append(f"{refused} linked Evidence item(s) did not answer it")
            elif check_failed:
                reasons.append("the check that its Evidence answers it failed")
        unjudged = sum(1 for aspect, reqs in unverifiable
                       if (aspect in req.aspect_ids if ledger.origin == "aspects"
                           else req.id in reqs))
        if unjudged:
            reasons.append(f"{unjudged} candidate fact(s) could not be checked (the support "
                           "check failed)")
        if answers is not None and answers.unsure.get(req.id):
            reasons.append(f"the answer check was unsure whether {answers.unsure[req.id]} "
                           "statement(s) answer it")
        omitted = sum(1 for reqs in cut if req.id in reqs)
        if omitted:
            reasons.append(f"{omitted} verified Evidence item(s) linked to it were left out to "
                           "fit the Evidence budget")
        failed = [t.id for t in tasks if t.id in failed_subtasks]
        if failed:
            reasons.append(f"Sub-task(s) {', '.join(failed)} failed")
        lost = sorted({s for a in req.aspect_ids for s in found_by.get(a, set())} & lost_sources)
        if lost:
            n = sum(len(u.passages) for u in extraction.failed_units if u.source_id in lost)
            reasons.append(f"{n} Passage(s) of {len(lost)} Source(s) could not be read after "
                           "bounded retries")
        good = [i for i in items if not i.conflicting]
        parts = (answers.parts.get(req.id, []) if answers else [])
        stated = {k for i in good for k in (answers.states.get(req.id, {}).get(i.id, [])
                                            if answers else [])}
        missing = [p for k, p in enumerate(parts, start=1) if k not in stated]
        if good and missing:
            status: Status = "partial"
        elif good:
            status = "supported"
        elif items:
            status = "contradictory"
            reasons.append("its Evidence conflicts")
        elif any(a in background_aspects for a in req.aspect_ids) and all(
                t.source_type == "memory" for t in tasks):
            status = "background"
        elif failed or lost:
            status = "incomplete"
        elif check_failed or unjudged:
            status = "verification_incomplete"
        elif omitted:
            status = "budget_omitted"
        else:
            status = "no_evidence"
        out.append(RequirementStatus(id=req.id, question=req.question, priority=req.priority,
                                     status=status, evidence_ids=[i.id for i in items],
                                     reasons=reasons,
                                     missing_parts=missing if status == "partial" else []))
    return out


def mark_reported(statuses: list[RequirementStatus], draft: ReportDraft) -> list[RequirementStatus]:
    """After writing: a supported or contradictory requirement whose item a current Claim cites
    (as primary or premise) is reported, with those Claims."""
    cited: dict[str, list[str]] = {}
    for si, s in enumerate(draft.sections):
        for ci, c in enumerate(s.claims):
            if not c.historical:
                for eid in c.evidence_ids():
                    cited.setdefault(eid, []).append(f"{si}.{ci}")
    out = []
    for st in statuses:
        claims = list(dict.fromkeys(p for e in st.evidence_ids for p in cited.get(e, [])))
        status = "reported" if claims and st.status in ("supported", "contradictory") else st.status
        out.append(st.model_copy(update={"status": status, "claims": claims}))
    return out


def after_fact_check(statuses: list[RequirementStatus],
                     verdicts: list[ClaimVerdict]) -> list[RequirementStatus]:
    """After fact-checking: each sampled Claim's outcome on the requirements it answers, and a
    reported requirement whose every Claim was sampled and judged unverified becomes
    `reported_unverified`. Page drift (the citation supported the Claim; the page no longer
    does) and unchecked Claims are recorded but never count as a failed citation."""
    outcome = {f"{v.section}.{v.claim}": (
        "page_drift" if v.verdict == "unverified" and v.cited_support == "SUPPORTED"
        else v.verdict) for v in verdicts}
    out = []
    for st in statuses:
        checked = {c: outcome[c] for c in st.claims if c in outcome}
        failed = st.status == "reported" and st.claims and all(
            checked.get(c) == "unverified" for c in st.claims)
        out.append(st.model_copy(update={
            "fact_check": checked, "status": "reported_unverified" if failed else st.status}))
    return out


def unreported_supported(statuses: list[RequirementStatus], draft: ReportDraft) -> list[RequirementStatus]:
    """Required, supported requirements no current Claim of `draft` answers."""
    return [s for s in mark_reported(statuses, draft)
            if s.priority == "required" and s.status == "supported"]


_WHY = {"contradictory": "the Evidence found conflicts",
        "incomplete": "the research for it did not complete",
        "verification_incomplete": "Evidence for it could not be checked (a check failed)",
        "budget_omitted": "its verified Evidence was left out to fit the Evidence budget",
        "no_evidence": "no Evidence for it passed the support check",
        "background": "only earlier Reports bear on it"}


def gap_lines(statuses: list[RequirementStatus]) -> list[str]:
    """One fixed-wording line per requirement the Report does not answer with current
    Evidence, or whose answer failed the Fact-Checker. Never that a Source lacks something."""
    out = []
    for s in statuses:
        if s.status in ("supported", "reported"):
            continue
        detail = f" ({'; '.join(s.reasons)})" if s.reasons else ""
        if s.status == "partial":
            out.append(f"Requirement {s.id} ({s.question}): answered only in part; this research "
                       f"did not establish {', '.join(s.missing_parts)}{detail}.")
            continue
        if s.status == "reported_unverified":
            out.append(f"Requirement {s.id} ({s.question}): the Claim(s) answering it "
                       f"({', '.join(s.claims)}) failed the independent fact-check and are "
                       "marked [UNVERIFIED]; treat that answer as unconfirmed.")
            continue
        out.append(f"Requirement {s.id} ({s.question}): this research did not establish it; "
                   f"{_WHY[s.status]}{detail}.")
    return out


def coverage_index(statuses: list[RequirementStatus]) -> str:
    """The compact answer index the Writer is shown (outside the Evidence budget)."""
    lines = []
    for s in statuses:
        if s.status == "supported":
            what = "answered by " + ", ".join(s.evidence_ids[:6])
        elif s.status == "partial":
            what = ("answered in part by " + ", ".join(s.evidence_ids[:6]) + "; NOT established: "
                    + ", ".join(s.missing_parts) + "; say this research did not establish them")
        elif s.status == "contradictory":
            what = "conflicting evidence " + ", ".join(s.evidence_ids[:6]) + "; say so"
        else:
            what = f"NOT established ({_WHY[s.status]}); say this research did not establish it"
        lines.append(f"- {s.id} [{s.priority}] {s.question}: {what}")
    return "\n".join(lines)
