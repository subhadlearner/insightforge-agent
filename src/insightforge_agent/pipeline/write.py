"""WRITING: the Report draft, from the Evidence bundle and the Historical context alone.

The Writer never sees Passage text. Every Claim must cite an existing Evidence item and may
contain only numbers that item contains. A Historical Claim instead cites a Historical item (an
earlier Report's Claim), is marked historical, shows that Report's date inline and is held to
the same number and name rules against the earlier statement. Neither kind may pose as the
other. Violations are retried with the offending Claims listed as a constraint, up to
`writer_retries` times."""

import re

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import EvidenceBundle, HistoricalContext, ReportDraft
from insightforge_agent.domain.memory import report_date_text, without_report_date
from insightforge_agent.domain.passages import named_entities_in, numbers_in
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.errors import RunFailed

SYSTEM = """You write a cited intelligence report from an Evidence bundle. Reply with JSON only:
{"title": str, "summary": str, "sections": [{"heading": str, "claims":
[{"text": str, "evidence_id": str, "historical": bool}]}]}
Rules: every claim cites exactly one evidence_id from the bundle and states only what that
item says; every number and name in a claim must appear in the cited item; use one section
per bundle section, with its heading. Do not invent evidence ids.
Historical background, when given, comes from the user's earlier reports, not from current
research. A claim citing a historical item (id starting "hist_") must set "historical": true,
state only what that item says, and show the item's report date inline exactly as
"(reported YYYY-MM-DD)". Put it in the section with the item's heading. Never present
historical background as a current fact, and never set "historical": true on a bundle item."""


# Words that begin a sentence without naming anything. Kept to articles, determiners and
# pronouns; any other capitalised opening word must appear in the cited item.
_OPENERS = {"the", "a", "an", "this", "that", "these", "those", "it", "its", "their", "they"}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?:])\s+")


def _opening_names(text: str) -> set[str]:
    """Capitalised first words of each sentence, which `named_entities_in` skips."""
    found = set()
    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        words = sentence.split()
        word = words[0].strip("\"'()[],;:.!?") if words else ""
        if word.endswith(("'s", "\u2019s")):
            word = word[:-2]
        if len(word) > 1 and word[0].isupper() and word.casefold() not in _OPENERS:
            found.add(word)
    return found


def _words(text: str) -> set[str]:
    cleaned = text.replace("\u2019s", "").replace("'s", "")
    return {w.strip("\"'()[],;:.!?").casefold() for w in cleaned.split()}


def _grounding(text: str, statement: str, entity: str, cited: str) -> list[str]:
    """Numbers and names in a Claim that the statement it cites does not contain."""
    problems = []
    extra = numbers_in(text) - numbers_in(statement)
    if extra:
        problems.append(f"claim {text!r} has values {sorted(extra)} not in {cited}")
    known = _words(statement) | _words(entity)
    names = sorted(n for n in named_entities_in(text) | _opening_names(text)
                   if n.casefold() not in known)
    if names:
        problems.append(f"claim {text!r} has names {names} not in {cited}")
    return problems


def violations(draft: ReportDraft, bundle: EvidenceBundle,
               historical: HistoricalContext | None = None) -> list[str]:
    items = {i.id: i for s in bundle.sections for i in s.items}
    background = (historical or HistoricalContext()).by_id()
    problems = []
    claims = [c for s in draft.sections for c in s.claims]
    if not claims:
        problems.append("the report has no claims")
    for c in claims:
        if c.historical:
            h = background.get(c.evidence_id)
            if h is None:
                problems.append(
                    f"claim {c.text!r} is marked historical but {c.evidence_id!r} is "
                    + ("current evidence, never historical" if c.evidence_id in items
                       else "not a historical item"))
                continue
            day = report_date_text(h.report_date)
            if day not in c.text:
                problems.append(f"historical claim {c.text!r} does not show its report date "
                                f"inline as (reported {day})")
            problems += _grounding(without_report_date(c.text, h.report_date), h.statement,
                                   h.entity, f"historical item {h.id}")
            continue
        if c.evidence_id in background:
            problems.append(f"claim {c.text!r} cites historical item {c.evidence_id!r} as current "
                            "evidence; set \"historical\": true and show its report date")
            continue
        item = items.get(c.evidence_id)
        if item is None:
            problems.append(f"claim {c.text!r} cites unknown evidence_id {c.evidence_id!r}")
            continue
        problems += _grounding(c.text, item.statement, item.entity, f"evidence {item.id}")
    return problems


def render_bundle(bundle: EvidenceBundle) -> str:
    lines = []
    for s in bundle.sections:
        lines.append(f"## {s.heading} (confidence {s.confidence})")
        lines += [f"- [{i.id}] ({i.confidence}) {i.statement}" for i in s.items]
    return "\n".join(lines)


def render_historical(historical: HistoricalContext) -> str:
    """Background from earlier Reports, apart from the bundle: never shown as Evidence."""
    return "\n".join(
        f"- [{h.id}] ({h.heading}; reported {report_date_text(h.report_date)}; as of "
        f"{h.as_of_period}) {h.statement}" for h in historical.items)


def write_report(
    deps: Deps, owner_id: str, run_id: str, brief: str, bundle: EvidenceBundle, gaps: list[str],
    historical: HistoricalContext | None = None,
) -> ReportDraft:
    historical = historical or HistoricalContext()
    background = ""
    if historical.items:
        background = ("\n\nHistorical background (earlier reports, not current evidence):\n"
                      + render_historical(historical))
    constraint = ""
    last: list[str] = []
    for attempt in range(deps.writer_retries + 1):
        user = (f"Brief: {brief}\n\nEvidence bundle:\n{render_bundle(bundle)}{background}"
                f"{constraint}")
        try:
            draft = ask_json(deps.writer_model, SYSTEM, user, ReportDraft)
            last = violations(draft, bundle, historical)
        except BadModelReply as e:
            last = [f"the reply was not valid report JSON: {e.brief}"]
        if not last:
            return draft.model_copy(update={"gaps": gaps})
        deps.log(owner_id, run_id, "writer_violations", attempt=attempt + 1, problems=last)
        constraint = "\n\nYour previous draft broke these rules. Fix them:\n" + "\n".join(
            f"- {p}" for p in last)
    raise RunFailed(f"the Writer broke the citation rules after {deps.writer_retries} retries")
