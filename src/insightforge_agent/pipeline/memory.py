"""Memory in a Run (design.md sections 4 and 5): which earlier Reports a Run may use, which Run
summaries the Planner reads, how the Memory researcher's references become Historical items,
and the finalisation that makes a Report searchable before its Run is COMPLETE.

Historical items are rebuilt from the persisted Report alone: the Claim's text and its original
Passages come from the Report body, never from a model reply or from the `report_sections`
payload, which is only a retrieval index."""

from dataclasses import dataclass
from datetime import date

from insightforge_agent.domain.contracts import (
    EvidenceBundle,
    HistoricalContext,
    HistoricalItem,
    PassageRef,
    ReportDraft,
)
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.ids import derive_historical_id
from insightforge_agent.domain.memory import (
    MemoryRef,
    build_run_summary,
    memory_eligible,
    planner_summaries,
    without_report_date,
)
from insightforge_agent.domain.models import Report, Run, RunSummary
from insightforge_agent.domain.plan import SubTaskPlan
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.stores.report_sections import SectionEntry


def eligible_report_ids(deps: Deps, owner_id: str, watchlist_item_id: str | None) -> list[str]:
    """The User's Reports a new Run (of `watchlist_item_id`, or one-shot when None) may use as
    memory. Decides both memory availability at submission and what the Memory researcher may
    search."""
    out = []
    for report in deps.repos.reports.list_for_owner(owner_id):
        try:
            run = deps.repos.runs.get(owner_id, report.run_id)
            summary = deps.repos.summaries.get_for_run(owner_id, report.run_id)
        except NotFoundError:
            continue
        if report.owner_id == owner_id and memory_eligible(owner_id, run, summary,
                                                           watchlist_item_id):
            out.append(report.id)
    return out


def summaries_for_planner(deps: Deps, owner_id: str, watchlist_item_id: str | None,
                          current_run_id: str) -> list[RunSummary]:
    """The last 3 Run summaries for the same Watchlist Item, or for the User's one-shot Runs."""
    runs = {r.id: r for r in deps.repos.runs.list_for_owner(owner_id)}
    return planner_summaries(owner_id, deps.repos.summaries.list_for(owner_id, watchlist_item_id),
                             runs, watchlist_item_id, current_run_id)


@dataclass(frozen=True)
class Lineage:
    """Where one Claim of a persisted Report comes from, flattened to the Report that first made
    it and that Report's original Passages."""

    statement: str
    entity: str
    report_id: str
    run_id: str
    report_date: date
    as_of_period: str
    primary: PassageRef
    supporting: list[PassageRef]


def claim_lineage(report: Report, section: int, claim: int) -> Lineage | None:
    """The lineage of one Claim, or None when there is no such Claim, the Fact-Checker marked
    it unverified, or its Evidence or Historical item is missing from the Report."""
    body = report.body
    draft = ReportDraft.model_validate(body["draft"])
    if not (0 <= section < len(draft.sections) and 0 <= claim < len(draft.sections[section].claims)):
        return None
    if any(v["section"] == section and v["claim"] == claim and v["verdict"] == "unverified"
           for v in body.get("verified", {}).get("verdicts", [])):
        return None
    c = draft.sections[section].claims[claim]
    if c.historical:  # reuse the stored lineage: the original Report and Passages, not this one
        h = HistoricalContext.model_validate(body.get("historical") or {}).by_id().get(
            c.evidence_id)
        if h is None:
            return None
        return Lineage(without_report_date(c.text, h.report_date), h.entity, h.report_id,
                       h.run_id, h.report_date, h.as_of_period, h.primary, list(h.supporting))
    items = {i.id: i for s in EvidenceBundle.model_validate(body["evidence"]).sections
             for i in s.items}
    item = items.get(c.evidence_id)
    if item is None:
        return None
    return Lineage(c.text, item.entity, report.id, report.run_id, report.created_at.date(),
                   item.as_of_period, item.primary, list(item.supporting))


@dataclass(frozen=True)
class Resolved:
    context: HistoricalContext
    accepted: dict[str, list[MemoryRef]]  # per Sub-task, the references that became background
    rejected: dict[str, list[MemoryRef]]  # per Sub-task, those that could not


def resolve_historical(deps: Deps, owner_id: str, run_id: str, plan: SubTaskPlan,
                       refs_by_subtask: dict[str, list[MemoryRef]],
                       eligible: set[str]) -> Resolved:
    """Historical items for the references each memory Sub-task may use (the caller has
    already kept only those its own searches returned). A reference to a Report outside
    `eligible`, or to a Claim with no lineage, is rejected. An earlier Claim found by several
    Sub-tasks is one item, kept by the most important Sub-task."""
    names = {a.id: a.name for a in plan.aspects}
    items: dict[str, HistoricalItem] = {}
    seen: set[tuple] = set()
    accepted: dict[str, list[MemoryRef]] = {}
    rejected: dict[str, list[MemoryRef]] = {}
    for task in sorted(plan.sub_tasks, key=lambda t: (t.priority, t.id)):
        for ref in refs_by_subtask.get(task.id, []):
            lineage = None
            if ref.report_id in eligible:
                try:
                    lineage = claim_lineage(deps.repos.reports.get(owner_id, ref.report_id),
                                            ref.section, ref.claim)
                except NotFoundError:
                    lineage = None
            if lineage is None:
                rejected.setdefault(task.id, []).append(ref)
                continue
            accepted.setdefault(task.id, []).append(ref)
            same = (lineage.report_id, lineage.primary, lineage.statement)
            if same in seen:
                continue
            seen.add(same)
            hid = derive_historical_id(run_id, *ref)
            items[hid] = HistoricalItem(
                id=hid, subtask_id=task.id, aspect_id=task.aspect_id,
                heading=names.get(task.aspect_id, task.aspect_id), statement=lineage.statement,
                entity=lineage.entity, report_id=lineage.report_id, run_id=lineage.run_id,
                report_date=lineage.report_date, as_of_period=lineage.as_of_period,
                primary=lineage.primary, supporting=lineage.supporting)
    return Resolved(HistoricalContext(items=list(items.values())), accepted, rejected)


def section_entries(report: Report) -> list[SectionEntry]:
    """The Report's Claims to index: every Claim with a lineage, by its clean statement."""
    draft = ReportDraft.model_validate(report.body["draft"])
    out = []
    for si, s in enumerate(draft.sections):
        for ci in range(len(s.claims)):
            if (lineage := claim_lineage(report, si, ci)) is not None:
                out.append(SectionEntry(si, ci, s.heading, lineage.statement))
    return out


def _key_entities(deps: Deps, report: Report) -> list[str]:
    body = report.body
    found = [i.entity for s in EvidenceBundle.model_validate(body["evidence"]).sections
             for i in s.items]
    found += [h.entity for h in HistoricalContext.model_validate(body.get("historical") or {}).items]
    for fact in deps.repos.entities.list_for_run(report.owner_id, report.run_id):
        found += [fact.entity, *fact.entities]
    return [e for e in found if e]


def report_finalised(deps: Deps, owner_id: str, run_id: str) -> bool:
    """A Run's Report is finalised once its summary is stored: the last finalisation step."""
    try:
        deps.repos.summaries.get_for_run(owner_id, run_id)
    except NotFoundError:
        return False
    return True


def finalise_report(deps: Deps, run: Run, report: Report) -> RunSummary:
    """Index the Report's Claims in `report_sections`, then store the Run summary. Idempotent:
    the index upsert rewrites the same points and the first summary stored is kept. Must finish
    before the Report is stored and the Run is COMPLETE, so a COMPLETE Run is always searchable."""
    index = deps.report_index
    if index is not None:
        index.index(report.owner_id, report.id, report.run_id, report.created_at.date(),
                    section_entries(report))
    draft = ReportDraft.model_validate(report.body["draft"])
    summary = build_run_summary(
        owner_id=report.owner_id, run_id=run.id, watchlist_item_id=run.watchlist_item_id,
        created_at=deps.now(), brief=run.brief, themes=[s.heading for s in draft.sections],
        entities=_key_entities(deps, report), gaps=draft.gaps, memory_indexed=index is not None)
    stored = deps.repos.summaries.add(summary)
    deps.log(report.owner_id, run.id, "run_summary_stored", report_id=report.id,
             memory_indexed=stored.memory_indexed, themes=len(stored.themes))
    return stored
