"""WRITING: the Report draft, from the Evidence bundle and the Historical context alone.

The Writer never sees Passage text. Every Claim must cite an existing Evidence item and may
contain only numbers that item contains. A Historical Claim instead cites a Historical item (an
earlier Report's Claim), is marked historical, shows that Report's date in a recognised
annotation, sits in the section of its item's heading and is held to the same number and name
rules against the earlier statement. Every heading of the Historical context gets at least one
Historical Claim. Neither kind may pose as the other. Violations are retried with the offending
Claims listed as a constraint, up to `writer_retries` times.

Placement and coverage (T20 S1.1). A current Claim is filed under its Evidence item's section
heading in code, never by asking the Writer again, and every Evidence section must keep at least
one Claim. The Executive Summary is held to `SUMMARY_MAX_WORDS` and to a lexical grounding check:
each number in it must appear in some Evidence or Historical statement, and each name in that
material or the Brief. The check is lexical; it does not establish that the evidence entails the
summary. When retries run out on a draft whose Claims pass, the draft is repaired in code
instead of failing the Run: an uncovered section is filled with its items' own statements, an
unsupported summary sentence is removed (an emptied summary is rebuilt from the Claims), and an
over-long summary is shortened in code to whole sentences or whole Claims (`fit_summary`), so a
published summary never exceeds the limit. Claim-rule violations still fail the Run."""

import re

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    HistoricalContext,
    ReportDraft,
    ReportSection,
)
from insightforge_agent.domain.memory import (
    report_date_text,
    strip_report_date,
    without_report_date,
)
from insightforge_agent.domain.passages import named_entities_in, numbers_in
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.errors import RunFailed

SYSTEM = """You write a cited intelligence report from an Evidence bundle. Reply with JSON only:
{"title": str, "summary": str, "sections": [{"heading": str, "claims":
[{"text": str, "evidence_id": str, "historical": bool}]}]}
Rules: every claim cites exactly one evidence_id and states only what that item says; every
number and name in a claim must appear in the cited item. Do not invent evidence ids.
Current evidence: write one section per bundle section, with its heading, holding claims that
cite that section's items with "historical": false.
Historical background, when given, comes from the user's earlier reports, not from current
research. Each item shows its heading. A claim citing a historical item (id starting "hist_")
must set "historical": true, state only what that item says, and show the item's report date
inline exactly as "(reported YYYY-MM-DD)". Put it in the section whose heading is exactly the
item's heading: the bundle section with that heading if there is one, otherwise a section of
its own with that heading. Every heading in the historical background needs at least one
historical claim; you need not use every historical item. Never present historical
background as a current fact, and never set "historical": true on a bundle item.
Summary: an executive summary of at most 150 words. Every number and name in it must appear
in the evidence bundle or the historical background; do not add totals, rates or names the
evidence does not state."""

SUMMARY_MAX_WORDS = 150


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


def _heading_key(heading: str) -> str:
    return " ".join(heading.split()).casefold()


def violations(draft: ReportDraft, bundle: EvidenceBundle,
               historical: HistoricalContext | None = None) -> list[str]:
    items = {i.id: i for s in bundle.sections for i in s.items}
    background = (historical or HistoricalContext()).by_id()
    problems = []
    covered: set[str] = set()  # historical headings with a well-placed Historical Claim
    if not any(s.claims for s in draft.sections):
        problems.append("the report has no claims")
    for section in draft.sections:
        for c in section.claims:
            if c.historical:
                h = background.get(c.evidence_id)
                if h is None:
                    problems.append(
                        f"claim {c.text!r} is marked historical but {c.evidence_id!r} is "
                        + ("current evidence, never historical" if c.evidence_id in items
                           else "not a historical item"))
                    continue
                if _heading_key(section.heading) == _heading_key(h.heading):
                    covered.add(_heading_key(h.heading))
                else:
                    problems.append(f"historical claim {c.text!r} is under {section.heading!r}; "
                                    f"it belongs in the section headed {h.heading!r}")
                if strip_report_date(c.text, h.report_date) is None:
                    problems.append(f"historical claim {c.text!r} does not show its report date "
                                    f"inline as (reported {report_date_text(h.report_date)})")
                problems += _grounding(without_report_date(c.text, h.report_date), h.statement,
                                       h.entity, f"historical item {h.id}")
                continue
            if c.evidence_id in background:
                problems.append(f"claim {c.text!r} cites historical item {c.evidence_id!r} as "
                                "current evidence; set \"historical\": true and show its report "
                                "date")
                continue
            item = items.get(c.evidence_id)
            if item is None:
                problems.append(f"claim {c.text!r} cites unknown evidence_id {c.evidence_id!r}")
                continue
            problems += _grounding(c.text, item.statement, item.entity, f"evidence {item.id}")
    headings = {_heading_key(h.heading): h.heading for h in background.values()}
    for key, heading in headings.items():
        if key not in covered:
            problems.append(f"the historical background headed {heading!r} has no historical "
                            f"claim; add at least one in a section headed {heading!r}")
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


def place_claims(draft: ReportDraft, bundle: EvidenceBundle) -> tuple[ReportDraft, list[dict]]:
    """File every current Claim under its Evidence item's section heading, keeping the Claims'
    order. Sections are merged by heading (case and spacing ignored), bundle sections come
    first in bundle order, and empty sections are dropped. Historical Claims and Claims citing
    an unknown id stay where the Writer put them, for `violations` to judge. Returns the new
    draft and one record per Claim moved."""
    home = {i.id: s.heading for s in bundle.sections for i in s.items}
    sections: dict[str, ReportSection] = {}
    for s in bundle.sections:
        sections.setdefault(_heading_key(s.heading), ReportSection(heading=s.heading, claims=[]))
    moved = []
    for section in draft.sections:
        for c in section.claims:
            heading = section.heading
            if not c.historical and c.evidence_id in home:
                heading = home[c.evidence_id]
                if _heading_key(heading) != _heading_key(section.heading):
                    moved.append({"evidence_id": c.evidence_id, "from": section.heading,
                                  "to": heading})
            key = _heading_key(heading)
            sections.setdefault(key, ReportSection(heading=heading, claims=[]))
            sections[key].claims.append(c)
    kept = [s for s in sections.values() if s.claims]
    return draft.model_copy(update={"sections": kept}), moved


def coverage_problems(draft: ReportDraft, bundle: EvidenceBundle) -> list[str]:
    """Evidence sections that no current Claim cites."""
    cited = {c.evidence_id for s in draft.sections for c in s.claims if not c.historical}
    return [f"the evidence section headed {s.heading!r} has no claims; add at least one claim "
            f"citing its items ({', '.join(i.id for i in s.items)})"
            for s in bundle.sections if not cited & {i.id for i in s.items}]


# Capitalised words that open a summary sentence without naming anything, beyond `_OPENERS`.
# A summary is held to the whole bundle's vocabulary, so a common word that also appears in the
# evidence passes anyway; this list covers the discourse words that usually do not.
_SUMMARY_OPENERS = _OPENERS | {
    "across", "additionally", "after", "against", "all", "also", "although", "among", "as",
    "at", "both", "but", "by", "collectively", "combined", "consequently", "conversely",
    "despite", "during", "each", "evidence", "finally", "first", "for", "from", "further",
    "furthermore", "given", "here", "however", "in", "key", "many", "meanwhile", "moreover",
    "most", "much", "nevertheless", "no", "not", "notably", "on", "one", "our", "over",
    "overall", "overview", "second", "several", "since", "some", "still", "such", "summary",
    "taken", "there", "third", "though", "thus", "to", "together", "under", "we", "where",
    "while", "with", "within", "yet",
}


def _summary_vocabulary(bundle: EvidenceBundle, historical: HistoricalContext,
                        brief: str) -> tuple[set[str], set[str]]:
    """The numbers and the words a summary may use: those of every Evidence and Historical
    statement, and for words also their entities and headings and the Brief."""
    statements = [i.statement for s in bundle.sections for i in s.items]
    statements += [h.statement for h in historical.items]
    labels = [i.entity for s in bundle.sections for i in s.items]
    labels += [s.heading for s in bundle.sections]
    labels += [h.entity for h in historical.items] + [h.heading for h in historical.items]
    numbers: set[str] = set()
    words: set[str] = set()
    for t in statements:
        numbers |= numbers_in(t)
        words |= _words(t)
    for t in labels + [brief]:
        words |= _words(t)
    return numbers, words


def _summary_sentences(summary: str) -> list[str]:
    return [t for t in _SENTENCE_SPLIT.split(summary.strip()) if t]


def unsupported_summary_sentences(summary: str, bundle: EvidenceBundle,
                                  historical: HistoricalContext | None = None,
                                  brief: str = "") -> list[tuple[str, str]]:
    """Each summary sentence holding a number or a name the evidence does not contain, with
    the reason. Lexical only: a sentence that passes is not thereby shown to be entailed."""
    numbers, words = _summary_vocabulary(bundle, historical or HistoricalContext(), brief)
    out = []
    for sentence in _summary_sentences(summary):
        reasons = []
        extra = numbers_in(sentence) - numbers
        if extra:
            reasons.append(f"values {sorted(extra)}")
        openers = {w for w in _opening_names(sentence) if w.casefold() not in _SUMMARY_OPENERS}
        names = sorted(n for n in named_entities_in(sentence) | openers
                       if n.casefold() not in words)
        if names:
            reasons.append(f"names {names}")
        if reasons:
            out.append((sentence, " and ".join(reasons)))
    return out


def summary_words(summary: str) -> int:
    return len(summary.split())


def summary_problems(draft: ReportDraft, bundle: EvidenceBundle,
                     historical: HistoricalContext | None = None, brief: str = "") -> list[str]:
    """An empty summary, unsupported summary sentences, and a summary over the word limit."""
    if not draft.summary.strip():
        return [f"the summary is empty; write an executive summary of at most "
                f"{SUMMARY_MAX_WORDS} words"]
    problems = [f"summary sentence {sentence!r} has {why} not in the evidence"
                for sentence, why in unsupported_summary_sentences(
                    draft.summary, bundle, historical, brief)]
    words = summary_words(draft.summary)
    if words > SUMMARY_MAX_WORDS:
        problems.append(f"the summary has {words} words; it may have at most "
                        f"{SUMMARY_MAX_WORDS}")
    return problems


def _fill_uncovered(draft: ReportDraft, bundle: EvidenceBundle) -> tuple[ReportDraft, list[str]]:
    """Give each uncovered Evidence section one Claim per item, stating the item verbatim."""
    cited = {c.evidence_id for s in draft.sections for c in s.claims if not c.historical}
    missing = [s for s in bundle.sections if not cited & {i.id for i in s.items}]
    if not missing:
        return draft, []
    extra = [ReportSection(heading=s.heading, claims=[
        Claim(text=i.statement, evidence_id=i.id) for i in s.items]) for s in missing]
    grown, _ = place_claims(draft.model_copy(update={"sections": draft.sections + extra}), bundle)
    return grown, [s.heading for s in missing]


def _leading_within_limit(sentences: list[str]) -> list[str]:
    """The longest run of leading sentences that fits the word limit."""
    kept: list[str] = []
    for sentence in sentences:
        if summary_words(" ".join([*kept, sentence])) > SUMMARY_MAX_WORDS:
            break
        kept.append(sentence)
    return kept


def _claim_texts(draft: ReportDraft) -> list[str]:
    """Current Claims, the first of each section first, then Historical Claims (dated as
    written), which a Report of Historical background only has alone."""
    current = [[c.text for c in s.claims if not c.historical] for s in draft.sections]
    ordered = [cs[0] for cs in current if cs] + [t for cs in current for t in cs[1:]]
    return ordered + [c.text for s in draft.sections for c in s.claims if c.historical]


def _claims_within_limit(draft: ReportDraft) -> list[str]:
    """Claims in `_claim_texts` order, each kept whole while it fits the word limit. Each
    already passed its own grounding check."""
    chosen: list[str] = []
    for text in _claim_texts(draft):
        if summary_words(" ".join([*chosen, text])) <= SUMMARY_MAX_WORDS:
            chosen.append(text)
    return chosen


def fit_summary(sentences: list[str], draft: ReportDraft) -> tuple[str, str | None]:
    """A summary of at most `SUMMARY_MAX_WORDS` from supported `sentences`, adding no words of
    its own, and how it was shortened: None (it fitted), "sentences" (leading whole sentences),
    "claims" (no sentence fitted, so whole Claims), or "truncated" (nothing whole
    fits: the first sentence, or else the first Claim, cut at a word with a closing "…")."""
    if summary_words(" ".join(sentences)) <= SUMMARY_MAX_WORDS and sentences:
        return " ".join(sentences), None
    if kept := _leading_within_limit(sentences):
        return " ".join(kept), "sentences"
    if claims := _claims_within_limit(draft):
        return " ".join(claims), "claims"
    first = sentences[0] if sentences else next(iter(_claim_texts(draft)), "")
    return " ".join([*first.split()[:SUMMARY_MAX_WORDS - 1], "…"]), "truncated"


def _repair(deps: Deps, owner_id: str, run_id: str, draft: ReportDraft, bundle: EvidenceBundle,
            historical: HistoricalContext, brief: str) -> ReportDraft:
    """A draft whose Claims pass, made publishable in code once retries are spent. The
    published summary never exceeds `SUMMARY_MAX_WORDS` and holds only supported sentences or
    Claims (or a word-boundary cut of one)."""
    draft, filled = _fill_uncovered(draft, bundle)
    unsupported = unsupported_summary_sentences(draft.summary, bundle, historical, brief)
    dropped = {sentence for sentence, _ in unsupported}
    supported = [t for t in _summary_sentences(draft.summary) if t not in dropped]
    summary, shortened = fit_summary(supported, draft)
    draft = draft.model_copy(update={"summary": summary})
    deps.log(owner_id, run_id, "writer_repaired", filled_sections=filled,
             removed_summary_sentences=sorted(dropped), summary_rebuilt=not supported,
             summary_shortened=shortened, summary_words=summary_words(summary))
    if (violations(draft, bundle, historical) or coverage_problems(draft, bundle)
            or summary_words(summary) > SUMMARY_MAX_WORDS):
        raise RunFailed("the Writer's draft could not be repaired")
    return draft


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
    usable: ReportDraft | None = None  # the latest draft whose Claims pass, for `_repair`
    for attempt in range(deps.writer_retries + 1):
        user = (f"Brief: {brief}\n\nEvidence bundle:\n{render_bundle(bundle)}{background}"
                f"{constraint}")
        try:
            draft, moved = place_claims(ask_json(deps.writer_model, SYSTEM, user, ReportDraft),
                                        bundle)
            if moved:
                deps.log(owner_id, run_id, "writer_claims_placed", attempt=attempt + 1,
                         moved=moved)
            last = violations(draft, bundle, historical)
            if not last:
                usable = draft
            last += coverage_problems(draft, bundle)
            last += summary_problems(draft, bundle, historical, brief)
        except BadModelReply as e:
            last = [f"the reply was not valid report JSON: {e.brief}"]
        if not last:
            return draft.model_copy(update={"gaps": gaps})
        deps.log(owner_id, run_id, "writer_violations", attempt=attempt + 1, problems=last)
        constraint = "\n\nYour previous draft broke these rules. Fix them:\n" + "\n".join(
            f"- {p}" for p in last)
    if usable is not None:
        draft = _repair(deps, owner_id, run_id, usable, bundle, historical, brief)
        return draft.model_copy(update={"gaps": gaps})
    raise RunFailed(f"the Writer broke the citation rules after {deps.writer_retries} retries")
