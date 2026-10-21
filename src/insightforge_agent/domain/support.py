"""Evidence-bound support (ADR-0006): the deterministic half of the one contract Synthesis and
the Fact-Checker use to decide whether cited text supports one proposition.

A model proposes a verdict and a *support map*: the statement split into parts, each part with
the exact words of the cited text that state it, or (for the subject only) the identity of the
uploaded document the text belongs to. Code then checks what code can check, and nothing more:

1. Every excerpt occurs in the text the model was shown (`find_excerpt`): word for word, with
   case, punctuation, quotes and spacing ignored. A quote that is not there is a fabrication,
   so the judgment is unusable (`VERIFICATION_ERROR`), never support.
2. The parts cover the whole statement (`uncovered_terms`): every content word of the
   statement is in some part. A part the model left out is a part it did not show support for,
   which is how an unstated qualifier ("of cloud infrastructure services", "capex",
   "solid-state") is caught when the model does not map it.
3. Every figure of the statement is stated by the excerpts themselves (`unsupported_numbers`,
   exact quantities, so "$71 million" is "71 million dollars"): an amount, count or year cannot
   come from anywhere else.
4. An identity part is allowed only where identity context was given, carries no figure, and
   quotes the company that context names, in full or a run of its words (`identity_problem`):
   identity context is text shown to the judge, so like any excerpt it must be quoted, never
   replaced ("Medtronic" can never come from a Northwind document's identity). What else the
   part says ("the Northwind Health pipeline") is the model's judgment.

Meaning is the model's judgment: whether a part's excerpt states it (a paraphrase, an
equivalent unit, a negation, hedge, "only" or comparison), whether a part needs an excerpt at
all (framing such as "the earlier report found that"), and whether an identity part names the
document's company. Code does not decide meaning. Gate A (2026-10-21) found that the
operator word lists, the entity-word identity rule and the rule that every part needs an
excerpt caused 8 of its 9 wrong rejections of correct positives the model had accepted, among
them the Stage C six-programmes fact. They were retired; the identity rule keeps only its quotation
check (4). These checks never turn a
model's rejection into support."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from insightforge_agent.domain.quantities import unsupported_numbers

SupportVerdict = Literal["SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE",
                         "VERIFICATION_ERROR"]
# checks-3 (Gate A): checks 1-4 above; checks-2 added the operator lists and the identity word
# rule, checks-1 was the ADR-0006 contract. Recorded with every evaluation run.
CHECKS_VERSION = "checks-3"

# Function words: a statement's other words are its content, and each must be in some part of
# the map (the map spans the statement; whether a part is stated is the model's judgment).
_FUNCTION_WORDS = frozenset("""
a an the and or but of to in on at by for from with within into onto over under as then that
this these those it its it's their they them there here is are was were be been being has have
had do does did also both either each per via about around between among during while which who
whom whose what when where why how so such just very some other another same own s
""".split())

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# One figure: digits with separators and a decimal part, so "1,234.5" is one word, not three.
_FIGURE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _normal(token: str) -> str:
    return unicodedata.normalize("NFKC", token).casefold()


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """Normalised words and figures with their character offsets into `text` itself; figures
    lose their thousands separators. Each token is normalised on its own, so offsets stay
    those of the original text."""
    out: list[tuple[str, int, int]] = []
    at = 0
    while at < len(text):
        figure = _FIGURE.match(text, at)
        if figure and (at == 0 or not text[at - 1].isalnum()):
            out.append((figure.group().replace(",", "").rstrip("."), figure.start(), figure.end()))
            at = figure.end()
            continue
        word = _WORD.match(text, at)
        if word:
            out.append((_normal(word.group()), word.start(), word.end()))
            at = word.end()
        else:
            at += 1
    return out


def _words(text: str) -> list[str]:
    return [t for t, _, _ in _tokens(text)]


def _stem(word: str) -> str:
    """Plural and possessive forms count as the word ("programmes" covers "programme")."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def find_excerpt(excerpt: str, text: str) -> tuple[int, int] | None:
    """Where `excerpt` occurs in `text` as a run of whole words (case, punctuation, quotes and
    spacing ignored), as character offsets into `text`; None if it does not."""
    want = _words(excerpt)
    have = _tokens(text)
    if not want:
        return None
    n = len(want)
    for i in range(len(have) - n + 1):
        if all(have[i + k][0] == want[k] for k in range(n)):
            return have[i][1], have[i + n - 1][2]
    return None


def content_terms(statement: str) -> list[str]:
    """The statement's content words, in order, without repeats."""
    return list(dict.fromkeys(_stem(w) for w in _words(statement) if w not in _FUNCTION_WORDS))


def uncovered_terms(statement: str, parts: list[str]) -> list[str]:
    """Content words of the statement that no part contains."""
    covered = {_stem(w) for p in parts for w in _words(p)}
    return [w for w in content_terms(statement) if w not in covered]


def identity_problem(part: str, entity: str | None) -> str | None:
    """Why `part` may not be established by identity context, or None if it may: there must be
    an established entity, the part may hold no figure, and it must quote the entity's name or
    a run of its words. Whether the rest of the part is about that entity is the model's
    judgment."""
    if not entity:
        return f"identity used for {part!r} where no identity context was given"
    if any(c.isdigit() for c in part):
        return f"identity cannot establish a figure ({part!r})"
    words = entity.split()
    if not any(find_excerpt(" ".join(words[i:j]), part) is not None
               for i in range(len(words)) for j in range(i + 1, len(words) + 1)):
        return f"identity part {part!r} does not quote the document's entity {entity!r}"
    return None


@dataclass(frozen=True)
class SupportPart:
    part: str
    excerpts: tuple[str, ...] = ()
    identity: bool = False


@dataclass(frozen=True)
class SupportCheck:
    """The code's verdict on a model's SUPPORTED judgment of one cited text."""

    verdict: SupportVerdict  # SUPPORTED, INSUFFICIENT_EVIDENCE or VERIFICATION_ERROR
    problems: tuple[str, ...]
    spans: tuple[tuple[int, int], ...]  # each excerpt's offsets into the text it was checked on
    identity: tuple[str, ...]  # the parts established by identity context


def check_support(statement: str, parts: list[SupportPart], text: str,
                  entity: str | None) -> SupportCheck:
    """Checks 1-4 of the module docstring on a model's SUPPORTED judgment of `text`."""
    spans: list[tuple[int, int]] = []
    fabricated: list[str] = []
    problems: list[str] = []
    identity: list[str] = []
    excerpts: list[str] = []
    if not parts:
        return SupportCheck("INSUFFICIENT_EVIDENCE", ("no support map was given",), (), ())
    for p in parts:
        if p.identity:
            why = identity_problem(p.part, entity)
            if why:
                problems.append(why)
            else:
                identity.append(p.part)
            continue
        for e in p.excerpts:
            found = find_excerpt(e, text)
            if found is None:
                fabricated.append(e)
            else:
                spans.append(found)
                excerpts.append(e)
    if fabricated:
        return SupportCheck("VERIFICATION_ERROR",
                            tuple(f"excerpt not in the cited text: {e[:120]!r}" for e in fabricated),
                            tuple(spans), tuple(identity))
    missing = uncovered_terms(statement, [p.part for p in parts])
    if missing:
        problems.append(f"no part covers {missing}")
    figures = unsupported_numbers(statement, " \n ".join(excerpts))
    if figures:
        problems.append(f"no excerpt states {sorted(figures)}")
    return SupportCheck("INSUFFICIENT_EVIDENCE" if problems else "SUPPORTED", tuple(problems),
                        tuple(spans), tuple(identity))


@dataclass(frozen=True)
class JointPart:
    """A part of an analytical statement judged against several cited units together: each
    excerpt names the unit (1-based) it is quoted from; an identity part names its unit."""

    part: str
    excerpts: tuple[tuple[int, str], ...] = ()
    identity_unit: int | None = None


def check_joint(statement: str, parts: list[JointPart], texts: list[str],
                entities: list[str | None]) -> SupportCheck:
    """Checks 1-4 for a statement resting on several premises (ADR-0006 §6): every excerpt is in
    the unit it names, the parts cover the statement, the excerpts together state every figure,
    and identity is used only as `identity_problem` allows for the unit it names."""
    if not parts:
        return SupportCheck("INSUFFICIENT_EVIDENCE", ("no support map was given",), (), ())
    spans: list[tuple[int, int]] = []
    fabricated: list[str] = []
    problems: list[str] = []
    identity: list[str] = []
    excerpts: list[str] = []
    for p in parts:
        if p.identity_unit is not None:
            n = p.identity_unit
            why = (identity_problem(p.part, entities[n - 1]) if 1 <= n <= len(texts)
                   else f"identity part {p.part!r} names no cited unit")
            if why:
                problems.append(why)
            else:
                identity.append(p.part)
            continue
        for n, e in p.excerpts:
            found = find_excerpt(e, texts[n - 1]) if 1 <= n <= len(texts) else None
            if found is None:
                fabricated.append(e)
            else:
                spans.append(found)
                excerpts.append(e)
    if fabricated:
        return SupportCheck("VERIFICATION_ERROR",
                            tuple(f"excerpt not in the cited unit: {e[:120]!r}" for e in fabricated),
                            tuple(spans), tuple(identity))
    missing = uncovered_terms(statement, [p.part for p in parts])
    if missing:
        problems.append(f"no part covers {missing}")
    figures = unsupported_numbers(statement, " \n ".join(excerpts))
    if figures:
        problems.append(f"no excerpt states {sorted(figures)}")
    return SupportCheck("INSUFFICIENT_EVIDENCE" if problems else "SUPPORTED", tuple(problems),
                        tuple(spans), tuple(identity))
