"""Memory rules (design.md sections 4 and 5): Run summaries, which earlier Reports may feed the
Memory researcher, how its references are written, and the inline date of a Historical Claim.

Pure rules, no I/O. The one eligibility rule here decides both whether memory is available at
submission and which Reports the Memory researcher may search, so the two cannot disagree."""

import re
from datetime import date, datetime
from typing import NamedTuple

from insightforge_agent.domain.models import Run, RunState, RunSummary

PLANNER_SUMMARIES = 3  # the Planner reads at most this many earlier Run summaries
MAX_BRIEF_CHARS = 500
MAX_ITEM_CHARS = 200
MAX_THEMES = 6
MAX_ENTITIES = 12
MAX_GAPS = 6


def memory_eligible(owner_id: str, run: Run, summary: RunSummary | None,
                    watchlist_item_id: str | None) -> bool:
    """Whether the Report of `run` may feed a new Run of `owner_id` (for `watchlist_item_id`,
    or a one-shot Run when None): the User's own, from a COMPLETE Run (never a Failed Run),
    finalised with its Claims indexed for search, and in a Watchlist Run never from the same
    Watchlist Item, which must not confirm its own history."""
    return (run.owner_id == owner_id and run.state == RunState.COMPLETE
            and summary is not None and summary.owner_id == owner_id
            and summary.run_id == run.id and summary.memory_indexed
            and (watchlist_item_id is None or run.watchlist_item_id != watchlist_item_id))


def planner_summaries(owner_id: str, summaries: list[RunSummary], runs: dict[str, Run],
                      watchlist_item_id: str | None, current_run_id: str) -> list[RunSummary]:
    """The newest `PLANNER_SUMMARIES` summaries of the User's COMPLETE Runs for the same
    Watchlist Item, or of their one-shot Runs for a one-shot Run. `runs` maps run id to Run."""
    chosen = []
    for s in sorted(summaries, key=lambda s: (s.created_at, s.run_id), reverse=True):
        run = runs.get(s.run_id)
        if (s.owner_id == owner_id and s.run_id != current_run_id and run is not None
                and run.owner_id == owner_id and run.state == RunState.COMPLETE
                and s.watchlist_item_id == watchlist_item_id
                and run.watchlist_item_id == watchlist_item_id):
            chosen.append(s)
        if len(chosen) == PLANNER_SUMMARIES:
            break
    return chosen


def _clip(text: str, limit: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def _bounded(values: list[str], count: int) -> list[str]:
    return list(dict.fromkeys(c for v in values if (c := _clip(v, MAX_ITEM_CHARS))))[:count]


def build_run_summary(*, owner_id: str, run_id: str, watchlist_item_id: str | None,
                      created_at: datetime, brief: str, themes: list[str], entities: list[str],
                      gaps: list[str], memory_indexed: bool) -> RunSummary:
    """A bounded summary from structured records: no model call, no Report body."""
    return RunSummary(
        owner_id=owner_id, run_id=run_id, watchlist_item_id=watchlist_item_id,
        created_at=created_at, brief=_clip(brief, MAX_BRIEF_CHARS),
        themes=_bounded(themes, MAX_THEMES), key_entities=_bounded(entities, MAX_ENTITIES),
        gaps=_bounded(gaps, MAX_GAPS), memory_indexed=memory_indexed)


def render_run_summaries(summaries: list[RunSummary]) -> str:
    """What the Planner is shown of earlier Runs: the summaries only, newest first."""
    lines = []
    for s in summaries:
        lines.append(f"- Run of {s.created_at.date().isoformat()}: Brief: {s.brief}")
        if s.themes:
            lines.append(f"  Themes: {'; '.join(s.themes)}")
        if s.key_entities:
            lines.append(f"  Key entities: {', '.join(s.key_entities)}")
        if s.gaps:
            lines.append(f"  Gaps & limitations: {' | '.join(s.gaps)}")
    return "\n".join(lines)


class MemoryRef(NamedTuple):
    """One Claim of an earlier Report: what a `report_sections` hit names."""

    report_id: str
    section: int
    claim: int


_MEMORY_REF = re.compile(r"\b(rep_[A-Za-z0-9]+):s(\d+):c(\d+)\b")


def memory_ref_id(ref: MemoryRef) -> str:
    return f"{ref.report_id}:s{ref.section}:c{ref.claim}"


def memory_refs_in(text: str) -> list[MemoryRef]:
    """Memory references named in a reply, in order, without repeats."""
    return list(dict.fromkeys(
        MemoryRef(r, int(s), int(c)) for r, s, c in _MEMORY_REF.findall(text)))


def report_date_text(day: date) -> str:
    """How a Historical Claim must show its original date inline."""
    return day.isoformat()


def _annotations(day: date) -> tuple[str, ...]:
    """The recognised report-date annotations, and only these: the canonical inline note
    "(reported 2026-09-01)" and the leading clause "As reported on 2026-09-01, "."""
    iso = re.escape(report_date_text(day))
    return (rf"\s*\(reported {iso}\)", rf"^As reported on {iso},\s*")


def strip_report_date(text: str, day: date) -> str | None:
    """The Claim with its one recognised report-date annotation removed, or None when it has
    none. Any other mention of the date is substance, never an annotation, and stays."""
    for pattern in _annotations(day):
        stripped, n = re.subn(pattern, "", text, count=1)
        if n:
            return " ".join(stripped.split())
    return None


def without_report_date(text: str, day: date) -> str:
    """What the Writer's grounding and the Fact-Checker compare with the earlier statement: the
    Claim without its recognised annotation, or the whole Claim when it has none (so a bare date
    is checked as a figure of the Claim, never silently dropped)."""
    stripped = strip_report_date(text, day)
    return text if stripped is None else stripped
