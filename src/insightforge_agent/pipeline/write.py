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
each number in it must appear in an Evidence statement, and each name in the Evidence or the
Brief. Earlier-Report background may appear only in a sentence showing its report date, as a
Historical Claim does. The check is lexical; it does not establish that the evidence entails
the summary. When retries run out on a draft whose Claims pass, the draft is repaired in code
instead of failing the Run: an uncovered section gets one Claim stating its best-ranked item,
unsupported summary sentences are removed, and the rest is fitted to the limit from whole
sentences or whole Claims, never part of one, else replaced by a sentence that states no
finding (`fit_summary`). A published summary is never empty and never over the limit.
Claim-rule violations still fail the Run, with one conservative fallback (T18 Stage C): when no
draft's Claims pass, a current Claim that cites an existing Evidence item but adds a number or
name gets that item's exact statement as its text (`replace_ungrounded_claims`, logged as
`writer_claims_replaced`); an unknown Evidence id, a blank Claim or a Historical Claim's problem
is never repaired, and anything still wrong fails the Run.

Requirements and analysis (ADR-0006). The Writer is also shown a compact index of the Run's
research requirements (what the Brief asks, which items answer each, which were not
established), outside the Evidence budget. Every required, supported requirement must be
answered by a Claim citing one of its items; a draft that leaves one out is retried, and repair
fills it with the item's statement. An analytical Claim may rest on further items (`premises`):
its numbers and names must appear in one of its cited items, and it may not cite Historical
items.

Semantic checks before publication (`_checked_claims`, `_checked_summary`; PR #77 review). The
rules above are lexical: "revenue increased 20%" passes them for an item that says it declined
20%. So every Claim is checked against what it cites before it is published:
- a Claim whose text is its item's verified statement (the exact-reuse path: case, spacing and
  a final stop aside; for a Historical Claim, without its report-date annotation) needs no
  second judgment: Synthesis judged that statement against the same Passage;
- any other current or Historical Claim is judged by the support contract against its item's
  primary Passage, the one its citation shows; one that is not SUPPORTED is restated as the
  item's verified statement (or removed when another Claim already states it), logged as
  `writer_claims_checked`;
- an analytical Claim is judged against the primary Passages of all its items together
  (`judge_joint`); one that is not SUPPORTED is removed and logged.
Then every Executive Summary sentence that is not one of the published Claims verbatim is
judged jointly against the published Claims (never the whole bundle); one that is not
SUPPORTED is removed, so a rejected comparison cannot survive there, and a summary left empty is
rebuilt from whole Claims (`fit_summary`). A sentence that only says what this research did not
establish, with no figure, states no finding and is kept unjudged. The T18 absence measurement
(`summary_absence_flagged`) is taken on the Writer's summary before these checks: the flag
itself still removes nothing. Any failed check fails closed. Everything repair and
these checks do is recorded in the draft's `quality` flags: a flagged Report is degraded
output, not synthesis."""

import re

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    EvidenceItem,
    ExtractionResult,
    HistoricalContext,
    PassageRef,
    ReportDraft,
    ReportSection,
    ResearchResults,
)
from insightforge_agent.domain.document_recovery import FAILED_PREFIX
from insightforge_agent.domain.evidence import (
    DROP_LITERAL,
    DROP_NO_CITATION,
    DROP_UNSUPPORTED,
    DROP_UNVERIFIED,
)
from insightforge_agent.domain.memory import (
    report_date_text,
    strip_report_date,
    without_report_date,
)
from insightforge_agent.domain.passages import named_entities_in, numbers_in
from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.plan import SubTaskPlan
from insightforge_agent.domain.requirements import RequirementStatus, coverage_index
from insightforge_agent.domain.support import content_terms
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.errors import RunFailed

SYSTEM = """You write a cited intelligence report that answers a brief from an Evidence bundle.
Reply with JSON only:
{"title": str, "summary": str, "sections": [{"heading": str, "claims":
[{"text": str, "evidence_id": str, "premises": [str], "historical": bool}]}]}
Rules: every claim cites its main item as "evidence_id". A claim restating one item leaves
"premises" empty and states only what that item says. An analytical claim (a comparison, a
reconciliation of items that disagree, or a conclusion across items) lists the other items it
rests on in "premises", at most 3, and states only what follows directly from all of them. Every
number and name in a claim must appear in one of its cited items; never compute a new number
(no totals, differences, rates or growth figures the items do not state). Do not invent
evidence ids, and never use a historical item as a premise.
Answer the brief: the research requirements list what it asks and which items answer each.
Every answered requirement needs a claim citing one of its items. Where items disagree or
measure different things, say so and why in an analytical claim instead of listing them.
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
in the evidence bundle; do not add totals, rates or names the evidence does not state. A
summary sentence drawing on the historical background must show that item's report date
inline exactly as "(reported YYYY-MM-DD)", as its historical claim does.
Each evidence item shows its period ("as of ...") or, when none was stated, its source's
publication date; a publication date is not the period a figure measures.
When the evidence or the research gaps do not cover something the brief asks for, say that
this research did not establish it; never state that a source, document or company lacks or
does not disclose information.
Compare two figures directly only when they measure the same thing, in the same unit, for the
same period and scope; otherwise state each with its period and say why they are not directly
comparable. A comparison claim cites every item it compares."""
# T18 CS4: writer-2 added the temporal, absence and comparison rules (writer-1 before).
# ADR-0006: writer-3 adds premises, the requirement index and the no-new-numbers rule.
WRITER_PROMPT_VERSION = "writer-3"
MAX_PREMISES = 3

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
            if not c.text.strip():
                problems.append(f"the claim citing {c.evidence_id!r} under {section.heading!r} "
                                "has no text; state what the item says")
                continue
            if c.historical and c.premises:
                problems.append(f"historical claim {c.text!r} cannot have premises")
                continue
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
            premises = [p for p in c.premises if p != c.evidence_id]
            bad = [p for p in premises if p not in items]
            if bad:
                problems.append(f"claim {c.text!r} has premises {bad} that are not current "
                                "evidence items")
                continue
            if len(premises) > MAX_PREMISES:
                problems.append(f"claim {c.text!r} has {len(premises)} premises; at most "
                                f"{MAX_PREMISES}")
                continue
            cited = [item] + [items[p] for p in premises]
            problems += _grounding(c.text, " ".join(i.statement for i in cited),
                                   " ".join(i.entity for i in cited),
                                   "evidence " + ", ".join(i.id for i in cited))
    headings = {_heading_key(h.heading): h.heading for h in background.values()}
    for key, heading in headings.items():
        if key not in covered:
            problems.append(f"the historical background headed {heading!r} has no historical "
                            f"claim; add at least one in a section headed {heading!r}")
    return problems


def _when(item: EvidenceItem) -> str:
    """What an Evidence line says about time (T18 CS4): the stated period; or that none was
    stated, with the Source's publication date and, if so, that it may predate the window the
    Sub-task asked for. Never "outdated"; a publication date is never shown as the period."""
    if item.as_of_period != "UNKNOWN":
        if item.derived_from == "observation_date":
            return f"as of {item.as_of_period}, the date it was read; undated source"
        return f"as of {item.as_of_period}"
    when = "period not stated; " + (f"published {item.published_at:%Y-%m}"
                                    if item.published_at else "undated")
    return when + ("; may predate the requested window" if item.before_window else "")


def render_bundle(bundle: EvidenceBundle) -> str:
    """The bundle exactly as the Writer is shown it; the Evidence budget counts this text."""
    lines = []
    for s in bundle.sections:
        lines.append(f"## {s.heading} (confidence {s.confidence})")
        lines += [f"- [{i.id}] ({i.confidence}; {_when(i)}) {i.statement}" for i in s.items]
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


def coverage_problems(draft: ReportDraft, bundle: EvidenceBundle,
                      statuses: list[RequirementStatus] | None = None) -> list[str]:
    """Evidence sections that no current Claim cites, and required, supported research
    requirements that no current Claim answers (cites one of their items)."""
    cited = {e for s in draft.sections for c in s.claims if not c.historical
             for e in c.evidence_ids()}
    out = [f"the evidence section headed {s.heading!r} has no claims; add at least one claim "
           f"citing its items ({', '.join(i.id for i in s.items)})"
           for s in bundle.sections if not cited & {i.id for i in s.items}]
    kept = {i.id for s in bundle.sections for i in s.items}
    for r in unanswered(draft, statuses or []):
        ids = [e for e in r.evidence_ids if e in kept]
        out.append(f"the research requirement {r.id} ({r.question}) is answered by "
                   f"{', '.join(ids)} but no claim cites them; add a claim that answers it")
    return out


def unanswered(draft: ReportDraft, statuses: list[RequirementStatus]) -> list[RequirementStatus]:
    """Required, supported requirements no current Claim of `draft` cites an item of."""
    cited = {e for s in draft.sections for c in s.claims if not c.historical
             for e in c.evidence_ids()}
    return [r for r in statuses if r.priority == "required" and r.status == "supported"
            and r.evidence_ids and not cited & set(r.evidence_ids)]


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


def _vocabulary(statements: list[str], labels: list[str]) -> tuple[set[str], set[str]]:
    """The numbers of `statements`, and the words of `statements` and `labels`."""
    numbers: set[str] = set()
    words: set[str] = set()
    for t in statements:
        numbers |= numbers_in(t)
        words |= _words(t)
    for t in labels:
        words |= _words(t)
    return numbers, words


def _unsupported(sentence: str, numbers: set[str], words: set[str]) -> list[str]:
    """The numbers and names of `sentence` that the vocabulary does not contain."""
    reasons = []
    extra = numbers_in(sentence) - numbers
    if extra:
        reasons.append(f"values {sorted(extra)}")
    openers = {w for w in _opening_names(sentence) if w.casefold() not in _SUMMARY_OPENERS}
    names = sorted(n for n in named_entities_in(sentence) | openers if n.casefold() not in words)
    if names:
        reasons.append(f"names {names}")
    return reasons


def _summary_sentences(summary: str) -> list[str]:
    return [t for t in _SENTENCE_SPLIT.split(summary.strip()) if t]


UNDATED_BACKGROUND = "undated background"


def unsupported_summary_sentences(summary: str, bundle: EvidenceBundle,
                                  historical: HistoricalContext | None = None,
                                  brief: str = "") -> list[tuple[str, str]]:
    """Each summary sentence holding a number or a name the material it may draw on does not
    contain, with the reason. Lexical only: a sentence that passes is not thereby shown to be
    entailed.

    An undated sentence is current, so it may use only the Evidence bundle (statements,
    entities, headings) and the Brief. Background from earlier Reports keeps its T8 rule: a
    sentence may also use the Historical items of one report date only when it shows that
    date in a recognised annotation, `(reported YYYY-MM-DD)` or a leading `As reported on
    YYYY-MM-DD, `, and is checked without the annotation, as a Historical Claim is. An undated
    sentence that the background alone would support has the reason `UNDATED_BACKGROUND`."""
    historical = historical or HistoricalContext()
    labels = [i.entity for s in bundle.sections for i in s.items]
    labels += [s.heading for s in bundle.sections] + [h.heading for h in historical.items]
    numbers, words = _vocabulary([i.statement for s in bundle.sections for i in s.items],
                                 [*labels, brief])
    by_date: dict = {}
    for h in historical.items:
        by_date.setdefault(h.report_date, []).append(h)
    out = []
    for sentence in _summary_sentences(summary):
        reasons = _unsupported(sentence, numbers, words)
        if not reasons:
            continue
        dated = next(((day, stripped) for day in by_date
                      if (stripped := strip_report_date(sentence, day)) is not None), None)
        if dated is not None:
            day, stripped = dated
            past_n, past_w = _vocabulary([h.statement for h in by_date[day]],
                                         [h.entity for h in by_date[day]])
            reasons = _unsupported(stripped, numbers | past_n, words | past_w)
        elif historical.items:
            past_n, past_w = _vocabulary([h.statement for h in historical.items],
                                         [h.entity for h in historical.items])
            if not _unsupported(sentence, numbers | past_n, words | past_w):
                reasons = [UNDATED_BACKGROUND]
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
    problems = [f"summary sentence {sentence!r} presents earlier-Report background as current; "
                "show its report date inline as (reported YYYY-MM-DD), as its historical claim "
                "does" if why == UNDATED_BACKGROUND else
                f"summary sentence {sentence!r} has {why} not in the evidence"
                for sentence, why in unsupported_summary_sentences(
                    draft.summary, bundle, historical, brief)]
    words = summary_words(draft.summary)
    if words > SUMMARY_MAX_WORDS:
        problems.append(f"the summary has {words} words; it may have at most "
                        f"{SUMMARY_MAX_WORDS}")
    return problems


def _fill_uncovered(draft: ReportDraft, bundle: EvidenceBundle) -> tuple[ReportDraft, list[str]]:
    """Give each uncovered Evidence section one Claim, stating its first item verbatim: the
    bundle keeps a section's items best-ranked first. One is enough for coverage and adds as
    few Claims, and so Fact-Checker samples, as possible."""
    cited = {c.evidence_id for s in draft.sections for c in s.claims if not c.historical}
    missing = [s for s in bundle.sections if not cited & {i.id for i in s.items}]
    if not missing:
        return draft, []
    extra = [ReportSection(heading=s.heading, claims=[
        Claim(text=s.items[0].statement, evidence_id=s.items[0].id)]) for s in missing]
    grown, _ = place_claims(draft.model_copy(update={"sections": draft.sections + extra}), bundle)
    return grown, [s.heading for s in missing]


# Published only when no supported sentence and no whole Claim fits the word limit. It states
# no finding, so it cannot misstate one, and sends the reader to the cited sections.
NO_SUMMARY = ("No summary fitted the 150-word limit; the findings are set out in the "
              "sections below.")


def _leading_within_limit(sentences: list[str]) -> list[str]:
    """The longest run of leading sentences that fits the word limit."""
    kept: list[str] = []
    for sentence in sentences:
        if summary_words(" ".join([*kept, sentence])) > SUMMARY_MAX_WORDS:
            break
        kept.append(sentence)
    return kept


def _claim_texts(draft: ReportDraft) -> list[str]:
    """Current Claims, the first of each section first, then Historical Claims,
    which carry their report date as written and are all a Report of background only has."""
    current = [[c.text for c in s.claims if not c.historical] for s in draft.sections]
    ordered = [cs[0] for cs in current if cs] + [t for cs in current for t in cs[1:]]
    return ordered + [c.text for s in draft.sections for c in s.claims
                      if c.historical]


def _claims_within_limit(draft: ReportDraft) -> list[str]:
    """Claims in `_claim_texts` order, each kept whole while it fits the word limit. Each
    already passed its own grounding check."""
    chosen: list[str] = []
    for text in _claim_texts(draft):
        if summary_words(" ".join([*chosen, text])) <= SUMMARY_MAX_WORDS:
            chosen.append(text)
    return chosen


def fit_summary(sentences: list[str], draft: ReportDraft) -> tuple[str, str | None]:
    """A non-empty summary of at most `SUMMARY_MAX_WORDS`, built only from whole supported
    `sentences` or whole Claims, never part of one, and how it was made: None (the sentences
    fitted), "sentences" (leading whole sentences), "claims" (no sentence fitted, so whole
    Claims) or "placeholder" (nothing whole fits: `NO_SUMMARY`, which states no finding)."""
    sentences = [t for t in sentences if t.strip()]
    if sentences and summary_words(" ".join(sentences)) <= SUMMARY_MAX_WORDS:
        return " ".join(sentences), None
    if kept := _leading_within_limit(sentences):
        return " ".join(kept), "sentences"
    if claims := _claims_within_limit(draft):
        return " ".join(claims), "claims"
    return NO_SUMMARY, "placeholder"


def replace_ungrounded_claims(draft: ReportDraft, bundle: EvidenceBundle,
                              historical: HistoricalContext) -> tuple[ReportDraft, list[dict]] | None:
    """The conservative Claim fallback (T18 Stage C), once retries are spent: each current Claim
    that cites an existing Evidence item but states a number or name that item does not gets
    that item's exact statement as its text; its citation and section stay. Returns the draft
    and the replacements, or None when anything else is wrong (an unknown or invented Evidence
    id, a blank Claim, a Historical Claim's problem): those are never guessed at, and the Run
    fails as before."""
    items = {i.id: i for s in bundle.sections for i in s.items}
    replaced: list[dict] = []
    sections = []
    for si, section in enumerate(draft.sections):
        claims = []
        for ci, c in enumerate(section.claims):
            item = items.get(c.evidence_id)
            if (not c.historical and item is not None and c.text.strip()
                    and _grounding(c.text, item.statement, item.entity, f"evidence {item.id}")):
                replaced.append({"section": si, "claim": ci, "evidence_id": item.id,
                                 "original_text": c.text[:300]})
                c = c.model_copy(update={"text": item.statement, "premises": []})
            claims.append(c)
        sections.append(section.model_copy(update={"claims": claims}))
    draft = draft.model_copy(update={"sections": sections})
    if not replaced or violations(draft, bundle, historical):
        return None
    return draft, replaced


def _fill_requirements(draft: ReportDraft, bundle: EvidenceBundle,
                       statuses: list[RequirementStatus]) -> tuple[ReportDraft, list[str]]:
    """Answer each unanswered required requirement with one Claim stating its best-ranked kept
    item verbatim, filed under that item's section."""
    order = [i for s in bundle.sections for i in s.items]
    rank = {i.id: n for n, i in enumerate(order)}
    extra, filled = [], []
    for r in unanswered(draft, statuses):
        ids = sorted((e for e in r.evidence_ids if e in rank), key=rank.__getitem__)
        if ids:
            item = order[rank[ids[0]]]
            extra.append(ReportSection(heading="", claims=[Claim(text=item.statement,
                                                                evidence_id=item.id)]))
            filled.append(r.id)
    if not extra:
        return draft, []
    grown, _ = place_claims(draft.model_copy(update={"sections": draft.sections + extra}), bundle)
    return grown, filled


def _repair(deps: Deps, owner_id: str, run_id: str, draft: ReportDraft, bundle: EvidenceBundle,
            historical: HistoricalContext, brief: str, claims_replaced: int = 0,
            statuses: list[RequirementStatus] | None = None) -> ReportDraft:
    """A draft whose Claims pass, made publishable in code once retries are spent. The
    published summary is non-empty, at most `SUMMARY_MAX_WORDS`, and holds only whole
    supported sentences, whole Claims or `NO_SUMMARY`. What it did is in `quality`."""
    draft, filled = _fill_uncovered(draft, bundle)
    draft, answered = _fill_requirements(draft, bundle, statuses or [])
    unsupported = unsupported_summary_sentences(draft.summary, bundle, historical, brief)
    dropped = {sentence for sentence, _ in unsupported}
    supported = [t for t in _summary_sentences(draft.summary) if t not in dropped]
    summary, shortened = fit_summary(supported, draft)
    draft = draft.model_copy(update={"summary": summary})
    deps.log(owner_id, run_id, "writer_repaired", filled_sections=filled,
             filled_requirements=answered,
             removed_summary_sentences=sorted(dropped), summary_rebuilt=not supported,
             summary_shortened=shortened, summary_words=summary_words(summary),
             claims_replaced=claims_replaced)
    flags = ([f"claims_replaced_verbatim:{claims_replaced}"] if claims_replaced else []) + (
        [f"sections_filled_verbatim:{len(filled)}"] if filled else []) + (
        [f"requirements_filled_verbatim:{','.join(answered)}"] if answered else []) + (
        ["summary_rebuilt_from_claims"] if shortened in ("claims", "placeholder") else [])
    draft = draft.model_copy(update={"quality": [*draft.quality, *flags]})
    if (violations(draft, bundle, historical) or coverage_problems(draft, bundle, statuses)
            or not summary.strip() or summary_words(summary) > SUMMARY_MAX_WORDS):
        raise RunFailed("the Writer's draft could not be repaired")
    return draft


def write_report(
    deps: Deps, owner_id: str, run_id: str, brief: str, bundle: EvidenceBundle, gaps: list[str],
    historical: HistoricalContext | None = None,
    statuses: list[RequirementStatus] | None = None,
) -> ReportDraft:
    historical = historical or HistoricalContext()
    statuses = statuses or []
    background = ""
    if statuses:  # ADR-0006: the requirement index, outside the Evidence budget
        background += ("\n\nResearch requirements (what the brief asks; not evidence):\n"
                       + coverage_index(statuses))
    if gaps:  # T18 CS4: what this research did not establish, as the pipeline recorded it
        background += ("\n\nResearch gaps (stated by the pipeline):\n"
                       + "\n".join(f"- {g}" for g in gaps))
    if historical.items:
        background += ("\n\nHistorical background (earlier reports, not current evidence):\n"
                       + render_historical(historical))
    constraint = ""
    last: list[str] = []
    usable: ReportDraft | None = None  # the latest draft whose Claims pass, for `_repair`
    parsed: ReportDraft | None = None  # the latest draft at all, for the Claim fallback
    for attempt in range(deps.writer_retries + 1):
        user = (f"Brief: {brief}\n\nEvidence bundle:\n{render_bundle(bundle)}{background}"
                f"{constraint}")
        try:
            draft, moved = place_claims(ask_json(deps.writer_model, SYSTEM, user, ReportDraft),
                                        bundle)
            if moved:
                deps.log(owner_id, run_id, "writer_claims_placed", attempt=attempt + 1,
                         moved=moved)
            parsed = draft
            last = violations(draft, bundle, historical)
            if not last:
                usable = draft
            last += coverage_problems(draft, bundle, statuses)
            last += summary_problems(draft, bundle, historical, brief)
        except BadModelReply as e:
            last = [f"the reply was not valid report JSON: {e.brief}"]
        if not last:
            _flag_absence(deps, owner_id, run_id, draft.summary)
            draft = _checked(deps, owner_id, run_id, draft, bundle, historical, brief, statuses)
            return draft.model_copy(update={"gaps": gaps})
        deps.log(owner_id, run_id, "writer_violations", attempt=attempt + 1, problems=last)
        constraint = "\n\nYour previous draft broke these rules. Fix them:\n" + "\n".join(
            f"- {p}" for p in last)
    replaced: list[dict] = []
    if usable is None and parsed is not None:
        fallback = replace_ungrounded_claims(parsed, bundle, historical)
        if fallback is not None:
            usable, replaced = fallback
            # A replaced Claim restates its item verbatim: grounded, but neither independently
            # verified by this step nor the Writer's synthesis.
            deps.log(owner_id, run_id, "writer_claims_replaced", replaced=replaced)
    if usable is not None:
        draft = _repair(deps, owner_id, run_id, usable, bundle, historical, brief, len(replaced),
                        statuses)
        _flag_absence(deps, owner_id, run_id, draft.summary)
        draft = _checked(deps, owner_id, run_id, draft, bundle, historical, brief, statuses)
        return draft.model_copy(update={"gaps": gaps})
    raise RunFailed(f"the Writer broke the citation rules after {deps.writer_retries} retries")


def _checked(deps: Deps, owner_id: str, run_id: str, draft: ReportDraft, bundle: EvidenceBundle,
             historical: HistoricalContext, brief: str,
             statuses: list[RequirementStatus]) -> ReportDraft:
    """The semantic checks before publication (module docstring): Claims, then the summary
    against the Claims that survived."""
    draft = _checked_claims(deps, owner_id, run_id, draft, bundle, historical, brief, statuses)
    return _checked_summary(deps, owner_id, run_id, draft, bundle, historical)


def _norm(text: str) -> str:
    return " ".join(text.split()).rstrip(".").casefold()


def _same(a: str, b: str) -> bool:
    """The exact-reuse test: equal but for case, spacing and a final stop."""
    return _norm(a) == _norm(b)


def _supported(deps: Deps, owner_id: str, run_id: str, statement: str, ref: PassageRef,
               **where) -> tuple[str, list[str]]:
    """The support judgment of `statement` against the Passage `ref` (in windows if long):
    SUPPORTED if any window is. Any failure fails closed as VERIFICATION_ERROR."""
    from insightforge_agent.pipeline.support import cited_units, judge_units

    try:
        judged = judge_units(deps, owner_id, run_id, statement,
                             cited_units(deps, owner_id, statement, [ref]), **where)
    except RunFailed:  # the Run's token cap: stop the Run, never skip the check quietly
        raise
    except Exception as e:  # noqa: BLE001 - unchecked is never published as checked
        return "VERIFICATION_ERROR", [f"{type(e).__name__}: {e}"[:200]]
    verdicts = [j.verdict for j in judged]
    if "SUPPORTED" in verdicts:
        return "SUPPORTED", []
    return (verdicts[0] if verdicts else "VERIFICATION_ERROR",
            [p for j in judged for p in j.problems][:4])


def _checked_claims(deps: Deps, owner_id: str, run_id: str, draft: ReportDraft,
                    bundle: EvidenceBundle, historical: HistoricalContext, brief: str,
                    statuses: list[RequirementStatus]) -> ReportDraft:
    """Every Claim checked against what it cites (module docstring). A reworded Claim that is
    not SUPPORTED is restated as its item's verified statement, or removed when another Claim
    already states it; an analytical Claim that is not SUPPORTED is removed. The draft is
    repaired if that left a section or requirement unanswered."""
    from insightforge_agent.pipeline.support import cited_units, judge_joint

    items = {i.id: i for s in bundle.sections for i in s.items}
    background = historical.by_id()
    stated = {_norm(c.text) for s in draft.sections for c in s.claims}
    judged, rejected, analytical = 0, [], []
    sections = []
    for si, section in enumerate(draft.sections):
        claims = []
        for ci, c in enumerate(section.claims):
            if c.premises and not c.historical:
                cited = [items[e] for e in c.evidence_ids()]
                try:
                    units = cited_units(deps, owner_id, c.text, [i.primary for i in cited])
                    joint = judge_joint(deps, owner_id, run_id, c.text, units, section=si,
                                        claim=ci)
                    verdict, problems = joint.verdict, list(joint.problems)
                except RunFailed:
                    raise
                except Exception as e:  # noqa: BLE001 - fail closed
                    verdict, problems = "VERIFICATION_ERROR", [f"{type(e).__name__}"]
                if verdict == "SUPPORTED":
                    claims.append(c)
                else:
                    analytical.append({"section": si, "claim": ci,
                                       "evidence_ids": c.evidence_ids(), "verdict": verdict,
                                       "problems": problems})
                continue
            if c.historical:
                h = background[c.evidence_id]
                text, statement, ref = (without_report_date(c.text, h.report_date), h.statement,
                                        h.primary)
                verbatim = (f"{h.statement.rstrip().rstrip('.')} "
                            f"(reported {report_date_text(h.report_date)}).")
            else:
                item = items[c.evidence_id]
                text, statement, ref, verbatim = c.text, item.statement, item.primary, item.statement
            if _same(text, statement):
                claims.append(c)
                continue
            judged += 1
            verdict, problems = _supported(deps, owner_id, run_id, text, ref, section=si,
                                           claim=ci, evidence_id=c.evidence_id)
            if verdict == "SUPPORTED":
                claims.append(c)
                continue
            key = _norm(verbatim)
            action = "removed" if key in stated else "restated"
            rejected.append({"section": si, "claim": ci, "evidence_id": c.evidence_id,
                             "verdict": verdict, "problems": problems, "action": action,
                             "original_text": c.text[:300]})
            if action == "restated":
                stated.add(key)
                claims.append(c.model_copy(update={"text": verbatim}))
        sections.append(section.model_copy(update={"claims": claims}))
    deps.log(owner_id, run_id, "writer_claims_checked", judged=judged, rejected=rejected)
    if analytical:
        deps.log(owner_id, run_id, "analytical_claim_rejected", rejected=analytical)
    if not rejected and not analytical:
        return draft
    restated = sum(r["action"] == "restated" for r in rejected)
    removed = len(rejected) - restated
    flags = ([f"claims_restated_verbatim:{restated}"] if restated else []) + (
        [f"claims_removed:{removed}"] if removed else []) + (
        [f"analytical_claims_removed:{len(analytical)}"] if analytical else [])
    draft = draft.model_copy(update={"sections": [s for s in sections if s.claims],
                                     "quality": [*draft.quality, *flags]})
    if coverage_problems(draft, bundle, statuses) or unsupported_summary_sentences(
            draft.summary, bundle, historical, brief):
        draft = _repair(deps, owner_id, run_id, draft, bundle, historical, brief,
                        statuses=statuses)
    return draft


def _claim_units(sentence: str, texts: list[str], cap: int):
    """The published Claims a summary sentence is judged against: those sharing most of its
    content words first, as many as fit one call."""
    from insightforge_agent.pipeline.support import Unit, request_tokens

    terms = set(content_terms(sentence))
    ranked = sorted(range(len(texts)),
                    key=lambda n: (-len(terms & set(content_terms(texts[n]))), n))
    units: list = []
    for n in ranked:
        unit = Unit(PassageRef(observation_id="claim", index=n), texts[n])
        if request_tokens(sentence, units + [unit]) > cap:
            break
        units.append(unit)
    return units


def _checked_summary(deps: Deps, owner_id: str, run_id: str, draft: ReportDraft,
                     bundle: EvidenceBundle, historical: HistoricalContext) -> ReportDraft:
    """Each summary sentence that is not a published Claim verbatim, judged against the
    published Claims together; one that is not SUPPORTED is removed (module docstring)."""
    from insightforge_agent.pipeline.support import judge_joint

    texts = [c.text for s in draft.sections for c in s.claims]
    kept, removed, judged, limits = [], [], 0, 0
    for n, sentence in enumerate(_summary_sentences(draft.summary)):
        if any(_same(sentence, t) for t in texts):
            kept.append(sentence)
            continue
        if _EVIDENCE_QUALIFIED.search(sentence) and not numbers_in(sentence):
            kept.append(sentence)  # a stated limit of this research, not a finding
            limits += 1
            continue
        judged += 1
        units = _claim_units(sentence, texts, deps.passage_token_cap)
        try:
            verdict = judge_joint(deps, owner_id, run_id, sentence, units,
                                  summary_sentence=n).verdict if units else "INSUFFICIENT_EVIDENCE"
        except RunFailed:
            raise
        except Exception:  # noqa: BLE001 - fail closed
            verdict = "VERIFICATION_ERROR"
        (kept if verdict == "SUPPORTED" else removed).append(sentence)
    deps.log(owner_id, run_id, "summary_checked", judged=judged, removed=removed,
             limitations=limits)
    if not removed:
        return draft
    summary, how = fit_summary(kept, draft)
    flags = [f"summary_sentences_removed:{len(removed)}"] + (
        ["summary_rebuilt_from_claims"] if how in ("claims", "placeholder")
        and "summary_rebuilt_from_claims" not in draft.quality else [])
    return draft.model_copy(update={"summary": summary, "quality": [*draft.quality, *flags]})


# T18 CS4: a summary sentence asserting that a source lacks something. Measured, never removed.
_ABSENCE = re.compile(r"\b(no|only|not|lacks?|lacking|without|absent|none)\b", re.IGNORECASE)
_ABOUT_SOURCES = re.compile(r"\b(evidence|sources?|documents?|report|filings?|disclos\w*)\b",
                            re.IGNORECASE)
_EVIDENCE_QUALIFIED = re.compile(r"\bresearch (?:did|does) not establish\b", re.IGNORECASE)


def absence_sentences(summary: str) -> list[str]:
    """Summary sentences that assert an absence about the evidence or a source, unless worded
    as what this research did not establish. A measurement for review, not a validator."""
    return [s for s in _SENTENCE_SPLIT.split(summary.strip())
            if _ABSENCE.search(s) and _ABOUT_SOURCES.search(s)
            and not _EVIDENCE_QUALIFIED.search(s)]


def _flag_absence(deps: Deps, owner_id: str, run_id: str, summary: str) -> None:
    found = absence_sentences(summary)
    if found:
        deps.log(owner_id, run_id, "summary_absence_flagged", sentences=found)


_PROVENANCE_DROP = {DROP_LITERAL, DROP_NO_CITATION}


def research_gaps(*, plan: SubTaskPlan, research: ResearchResults, bundle: EvidenceBundle,
                  events: list[dict], documents: list[tuple[str, list[Passage]]],
                  extraction: ExtractionResult | None = None) -> list[str]:
    """What this Run did not establish, from its own records, in fixed wording (T18 CS4):
    a failed Sub-task with its reported reason; a recovered documents Sub-task with what was
    kept; a Sub-task whose material produced no kept Evidence, with why; a Sub-task some of whose
    Passages could not be read after bounded retries (ADR-0006 §4), whatever else it kept; and
    the sections of an uploaded document no Sub-task read. Never that a Source lacks something,
    and never that retrieved material was verified."""
    by_type: dict[str, list[dict]] = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e["payload"])

    def logged(type_: str, sid: str) -> list[dict]:
        return [p for p in by_type.get(type_, []) if p.get("subtask_id") == sid]

    results = {r.subtask_id: r for r in research.results}
    with_items = {i.subtask_id for s in bundle.sections for i in s.items}
    lines = []
    for t in sorted(plan.sub_tasks, key=lambda t: (t.priority, t.id)):
        r, head = results.get(t.id), f"Sub-task {t.id} ({t.query})"
        if r is not None and r.scope_origin == "search_recovery":
            outcome = (logged("document_recovery_outcome", t.id) or [{}])[-1]
            kept = outcome.get("retained_items", 0)
            lines.append(
                f"{head}: the Document Reader failed (its search results showed no chunk text); "
                f"{outcome.get('recovered_chunks', 0)} chunk(s) were recovered from its own "
                "searches as candidates" + (
                    f", and {kept} Evidence item(s) citing them were kept after the provenance, "
                    "figure and entailment checks." if kept else
                    "; none was kept after the provenance, figure and entailment checks."))
        elif r is None or r.status == "failed":
            # Fixed wording: the researcher's own words are model prose, and could claim that
            # a source lacks something; they stay in the Run log, never in the Report.
            reason = next((p.get("reason", "") for p in reversed(logged("subtask_failed", t.id))),
                          "")
            if reason.startswith(FAILED_PREFIX):
                lines.append(f"{head} failed: its researcher reported that it could not complete "
                             "it (the reply is kept in the Run log).")
            else:
                lines.append(f"{head} produced no usable sources.")
        elif t.source_type != "memory" and t.id not in with_items and logged("evidence_cut", t.id):
            lines.append(f"{head}: its {len(logged('evidence_cut', t.id))} Evidence item(s) were "
                         "cut by the evidence budget, below higher-ranked Evidence.")
        elif t.source_type != "memory" and t.id not in with_items:
            drops = logged("evidence_dropped", t.id)
            provenance = sum(p["reason"] in _PROVENANCE_DROP for p in drops)
            unsupported = sum(p["reason"] == DROP_UNSUPPORTED for p in drops)
            unchecked = sum(p["reason"] == DROP_UNVERIFIED for p in drops)
            failed_calls = len(logged("draft_batch_failed", t.id))  # T5-T18 Run logs
            line = f"{head}: this research did not establish it"
            if drops:
                line += (f"; {len(drops)} drafted item(s) were not kept ({provenance} failed the "
                         f"provenance or figure checks, {unsupported} not supported by the "
                         "entailment check"
                         + (f", {unchecked} could not be checked because the support check "
                            "failed" if unchecked else "") + ")")
            if failed_calls:
                line += f"{',' if drops else ';'} and {failed_calls} drafting call(s) failed"
            lines.append(line + ".")
    lost = (extraction or ExtractionResult()).failed_units
    for t in sorted(plan.sub_tasks, key=lambda t: (t.priority, t.id)):
        r = results.get(t.id)
        if r is None or r.status != "succeeded":
            continue
        mine = {s.source_id for s in r.source_refs}
        units = [u for u in lost if u.source_id in mine]
        if units:
            lines.append(f"Sub-task {t.id} ({t.query}): {sum(len(u.passages) for u in units)} "
                         f"Passage(s) in {len(units)} extraction batch(es) could not be read "
                         "after bounded retries (" + ", ".join(sorted({u.reason for u in units}))
                         + "); facts they may state were not considered.")
    used = {(p.observation_id, p.index) for r in research.results if r.status == "succeeded"
            for ref in r.source_refs for p in ref.passages}
    for title, passages in documents:
        unused: dict[str, int | None] = {}
        for p in passages:
            if (p.observation_id, p.index) not in used:
                unused.setdefault(p.section_heading or f"chunk {p.index}", p.page)
        if unused:
            lines.append(f"{title}: not used by this Run: " + ", ".join(
                f"{h} (p{page})" if page else h for h, page in unused.items()) + ".")
    return lines
