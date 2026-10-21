"""The requirements ledger (ADR-0006 §5): what the Brief asks, and whether this Run answered it.

A requirement is one atomic question of the Brief, linked to the Aspects that research it.
Its status is decided in code from the Run's own records, never from similarity:

- supported: a kept Evidence item linked to it (explicitly, or through its Aspect when the
  ledger fell back to one requirement per Aspect) was verified by the support contract;
- contradictory: its only verified items conflict;
- incomplete: nothing verified, and a Sub-task researching it failed or a work unit covering
  its Sub-tasks' material could not be read after bounded recovery;
- no_evidence: nothing verified, and nothing failed;
- background: only Historical items from earlier Reports bear on it (memory Sub-tasks);
- reported (after writing): a Claim of the Report cites one of its items.

Everything not supported or reported becomes a fixed-wording gap line, so the Report's
"Gaps & limitations" always matches the ledger. A requirement is never "reported" because its
Aspect has some Claim: a Claim must cite an item linked to that requirement."""

from typing import Literal

from pydantic import BaseModel, Field

from insightforge_agent.domain.contracts import EvidenceItem, ExtractionResult, ReportDraft, ResearchResults
from insightforge_agent.domain.plan import SubTaskPlan

MAX_REQUIREMENTS = 12

Status = Literal["supported", "contradictory", "incomplete", "no_evidence", "background",
                 "reported"]


class Requirement(BaseModel):
    id: str
    question: str
    priority: Literal["required", "optional"] = "required"
    aspect_ids: list[str] = Field(default_factory=list)


class RequirementLedger(BaseModel):
    version: int = 1
    origin: Literal["brief", "aspects"] = "aspects"  # "aspects": the fallback, one per Aspect
    requirements: list[Requirement] = Field(default_factory=list)

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


def assess(ledger: RequirementLedger, plan: SubTaskPlan, research: ResearchResults,
           extraction: ExtractionResult, kept: list[EvidenceItem],
           background_aspects: set[str] | None = None) -> list[RequirementStatus]:
    """Each requirement's status before writing, from the kept (verified, in-budget) items."""
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
        items = [i for i in kept if req.id in links(i, ledger)]
        tasks = [t for t in plan.sub_tasks if t.aspect_id in req.aspect_ids]
        reasons: list[str] = []
        failed = [t.id for t in tasks if t.id in failed_subtasks]
        if failed:
            reasons.append(f"Sub-task(s) {', '.join(failed)} failed")
        lost = sorted({s for a in req.aspect_ids for s in found_by.get(a, set())} & lost_sources)
        if lost:
            n = sum(len(u.passages) for u in extraction.failed_units if u.source_id in lost)
            reasons.append(f"{n} Passage(s) of {len(lost)} Source(s) could not be read after "
                           "bounded retries")
        good = [i for i in items if not i.conflicting]
        if good:
            status: Status = "supported"
        elif items:
            status = "contradictory"
            reasons.append("its Evidence conflicts")
        elif any(a in background_aspects for a in req.aspect_ids) and all(
                t.source_type == "memory" for t in tasks):
            status = "background"
        elif reasons:
            status = "incomplete"
        else:
            status = "no_evidence"
        out.append(RequirementStatus(id=req.id, question=req.question, priority=req.priority,
                                     status=status, evidence_ids=[i.id for i in items],
                                     reasons=reasons))
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


def unreported_supported(statuses: list[RequirementStatus], draft: ReportDraft) -> list[RequirementStatus]:
    """Required, supported requirements no current Claim of `draft` answers."""
    return [s for s in mark_reported(statuses, draft)
            if s.priority == "required" and s.status == "supported"]


_WHY = {"contradictory": "the Evidence found conflicts",
        "incomplete": "the research for it did not complete",
        "no_evidence": "no Evidence for it passed the support check",
        "background": "only earlier Reports bear on it"}


def gap_lines(statuses: list[RequirementStatus]) -> list[str]:
    """One fixed-wording line per requirement the Report does not answer with current
    Evidence. Never that a Source lacks something."""
    out = []
    for s in statuses:
        if s.status in ("supported", "reported"):
            continue
        detail = f" ({'; '.join(s.reasons)})" if s.reasons else ""
        out.append(f"Requirement {s.id} ({s.question}): this research did not establish it; "
                   f"{_WHY[s.status]}{detail}.")
    return out


def coverage_index(statuses: list[RequirementStatus]) -> str:
    """The compact answer index the Writer is shown (outside the Evidence budget)."""
    lines = []
    for s in statuses:
        if s.status == "supported":
            what = "answered by " + ", ".join(s.evidence_ids[:6])
        elif s.status == "contradictory":
            what = "conflicting evidence " + ", ".join(s.evidence_ids[:6]) + "; say so"
        else:
            what = f"NOT established ({_WHY[s.status]}); say this research did not establish it"
        lines.append(f"- {s.id} [{s.priority}] {s.question}: {what}")
    return "\n".join(lines)
