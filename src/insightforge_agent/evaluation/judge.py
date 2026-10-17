"""Synthesis quality: one LLM-judge call per Report against a fixed rubric (design.md
section 11).

The judge scores how well the Report synthesises, never whether it is true: factual accuracy
and citations are the deterministic metrics, and the judge cannot change them. It sees the
Brief, the expected Report structure and the Report as published (`body["verified"]["draft"]`,
so `[UNVERIFIED]` marks show), and no Passage text. A reply that is not a complete score sheet
of whole numbers from 1 to 5 is an `error` with no score; there is no retry, so every judged
Report had exactly one call. Changing the rubric text or criteria means a new RUBRIC_VERSION."""

from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import ReportDraft
from insightforge_agent.domain.models import Report
from insightforge_agent.evaluation.library import SampleBrief

RUBRIC_VERSION = "synthesis-rubric-v1"

CRITERIA = {
    "coverage": "Answers every part of the Brief and fills the expected sections, or says in "
                "Gaps & limitations why it cannot.",
    "organisation": "Sections are coherent and in a sensible order; the summary reflects the "
                    "body; Claims sit under the right heading.",
    "synthesis": "Connects and compares findings into an answer to the Brief, rather than "
                 "listing disconnected facts.",
    "calibration": "States uncertainty honestly: forecasts as forecasts, historical background "
                   "as background, conflicting or unverified material flagged, no overreach.",
}

SYSTEM = f"""You grade the synthesis quality of a research report. Rubric {RUBRIC_VERSION}.
Score each criterion with a whole number from 1 (poor) to 5 (excellent):
""" + "\n".join(f"- {name}: {text}" for name, text in CRITERIA.items()) + """
Do not judge factual accuracy or citations: those are measured separately. Judge only from the
brief, the expected structure and the report text you are given.
Reply with JSON only: {"coverage": int, "organisation": int, "synthesis": int,
"calibration": int, "rationale": str (at most 3 sentences)}"""

Score = Field(ge=1, le=5)


class ScoreSheet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: StrictInt = Score
    organisation: StrictInt = Score
    synthesis: StrictInt = Score
    calibration: StrictInt = Score
    rationale: str = ""


class SynthesisResult(BaseModel):
    status: Literal["judged", "error", "not_judged"]
    rubric_version: str = RUBRIC_VERSION
    scores: dict[str, int] | None = None
    score: float | None = None  # mean of the criteria, 1 to 5; None unless judged
    rationale: str = ""
    reason: str = ""  # why there is no score


def not_judged(reason: str) -> SynthesisResult:
    return SynthesisResult(status="not_judged", reason=reason)


def render_expected(sample: SampleBrief) -> str:
    lines = []
    for s in sample.expected_report_structure.sections:
        line = f"- {s.heading}"
        if s.must_cite:
            line += f" (should cover: {'; '.join(s.must_cite)})"
        if s.notes:
            line += f" [{' '.join(s.notes.split())}]"
        lines.append(line)
    return "\n".join(lines)


def render_report(draft: ReportDraft) -> str:
    lines = [f"Title: {draft.title}", f"Executive Summary: {draft.summary}"]
    for s in draft.sections:
        lines.append(f"## {s.heading}")
        lines += [f"- {'(historical) ' if c.historical else ''}{c.text}" for c in s.claims]
    lines.append("## Gaps & limitations")
    lines += [f"- {g}" for g in draft.gaps] or ["- (none stated)"]
    return "\n".join(lines)


def judge_synthesis(model: BaseChatModel, sample: SampleBrief, report: Report) -> SynthesisResult:
    try:
        draft = ReportDraft.model_validate(report.body["verified"]["draft"])
    except (KeyError, TypeError, ValidationError) as e:
        return SynthesisResult(status="error", reason=f"the Report has no published draft: {e!r}")
    user = (f"Brief: {sample.brief}\n\nExpected structure:\n{render_expected(sample)}\n\n"
            f"Report:\n{render_report(draft)}")
    try:
        sheet = ask_json(model, SYSTEM, user, ScoreSheet)
    except BadModelReply as e:
        return SynthesisResult(status="error", reason=f"invalid judge reply: {e.brief}")
    except Exception as e:  # noqa: BLE001 - a provider failure is recorded, never scored
        return SynthesisResult(status="error", reason=f"judge call failed: {type(e).__name__}: {e}")
    scores = {name: getattr(sheet, name) for name in CRITERIA}
    return SynthesisResult(status="judged", scores=scores,
                           score=sum(scores.values()) / len(scores), rationale=sheet.rationale)
