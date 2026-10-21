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
   names the document's own entity or a run of its words (`identity_problem`). It never names
   another company, so a fact about Medtronic can never become Northwind's this way.
5. Logical operators are held to the quotes (`operator_problems`), because they decide whether
   a statement is true and a support map can otherwise leave them out of every excerpt.
   Negation and hedging (expected, planned, may, will...) must agree between each part and its
   excerpts in both directions: a part that has one needs one in its excerpts; an excerpt that
   governs what the part asserts (the operator just before the quote, as in a cut-off "did not"
   or "is expected to", or within three words before a word of the part) needs one in the part.
   An operator elsewhere in a long quote ("expansion plans", "analyst estimates") governs
   nothing the part says and is ignored. Exclusivity ("only"), universality ("all") and, for a
   single cited unit, comparison ("more than", "highest") in a part must be stated by its
   excerpts. In a joint judgment the comparison is the analysis itself, so it is not required
   of the quotes.

Whether a part's excerpt *means* the part (a paraphrase, an equivalent unit, a genuine quote
attached to a part it does not state) stays the model's judgment: code does not decide meaning.
These checks are integrity backstops; they make a permissive judgment visible and fail it closed
where they can, and they never turn a model's rejection into support. Operator classes are word
lists: an operator worded outside them ("outpaced", "failed to") is the model's to judge, and a
faithful paraphrase that drops one ("did not grow" for "was flat") is refused, by design."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from insightforge_agent.domain.quantities import unsupported_numbers

SupportVerdict = Literal["SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE",
                         "VERIFICATION_ERROR"]

# Function words: a statement's other words are its content, and each must be in some part.
# Logical operators (not, no, only, all, any, more, than, will, may...) are content: a part map
# that leaves one out has not shown support for it.
_FUNCTION_WORDS = frozenset("""
a an the and or but of to in on at by for from with within into onto over under as then that
this these those it its it's their they them there here is are was were be been being has have
had do does did also both either each per via about around between among during while which who
whom whose what when where why how so such just very some other another same own s
""".split())

# Operator classes (check 5). Negation and hedging must agree both ways between a part and its
# excerpts; the others must be stated by the excerpts when the part states them.
_NEGATION = re.compile(r"\b(?:not|never|none|neither|nor|without|cannot|nothing|no(?!\.\s*\d))\b"
                       r"|n['’]t\b", re.IGNORECASE)
_HEDGE = re.compile(
    r"\b(?:will|would|shall|should|might|could|(?-i:may)|expects?|expected|expecting|"
    r"expectations?|plans?|planned|planning|intends?|intended|anticipates?|anticipated|"
    r"forecasts?|forecasted|projected|estimates?|estimated|targets|targeted|targeting|"
    r"proposes?|proposed|likely|unlikely|potential|potentially|possible|possibly|aims?|aimed|"
    r"aiming|guidance|outlook|seeks?|sought)\b", re.IGNORECASE)
_EXCLUSIVE = re.compile(r"\b(?:only|sole|solely|exclusively|alone)\b", re.IGNORECASE)
_UNIVERSAL = re.compile(r"\b(?:all|every|entire|always|any)\b", re.IGNORECASE)
_COMPARISON = re.compile(
    r"\b(?:more|less|fewer|than|most|least|greater|greatest|higher|highest|lower|lowest|"
    r"larger|largest|smaller|smallest|biggest|record)\b", re.IGNORECASE)
_BOTH_WAYS = (("negation", _NEGATION), ("hedging", _HEDGE))
_STATED = (("exclusivity", _EXCLUSIVE), ("universality", _UNIVERSAL))
_LEAD = 4  # words before a quote checked for a cut-off "not" or "expected to"
_REACH = 3  # an operator in a quote governs the next words up to this many

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
    an established entity, the part may hold no figure, and its content words must be the
    entity or a run of its words ("Northwind", "Northwind Health's")."""
    if not entity:
        return f"identity used for {part!r} where no identity context was given"
    if any(c.isdigit() for c in part):
        return f"identity cannot establish a figure ({part!r})"
    want = [_stem(w) for w in _words(part) if w not in _FUNCTION_WORDS]
    have = [_stem(w) for w in _words(entity)]
    if not want or not any(have[i:i + len(want)] == want
                           for i in range(len(have) - len(want) + 1)):
        return f"identity part {part!r} is not the document's entity {entity!r}"
    return None


def _lead(text: str, span: tuple[int, int]) -> str:
    """The words of `text` just before `span`, within its sentence: where a "not" or an
    "expected to" sits when a quote is cut to leave it out."""
    before = re.split(r"[.!?;:]\s", text[:span[0]])[-1]
    return " ".join(before.split()[-_LEAD:])


def _same_word(a: str, b: str) -> bool:
    """One word, allowing inflection: equal, or a common prefix of at least 4 letters and 60% of
    the shorter ("increase" and "increased"). Figures must be equal."""
    if a == b:
        return True
    if a[:1].isdigit() or b[:1].isdigit():
        return False
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n >= 4 and n >= 0.6 * min(len(a), len(b))


def _governs(pattern: re.Pattern, quote: str, terms: list[str]) -> bool:
    """Whether a word of `pattern` in `quote` comes within `_REACH` words before a word of
    `terms` (the part's own content words)."""
    words = [_stem(w) for w in _words(quote)]
    raw = quote.split()
    for i, w in enumerate(raw):
        if pattern.search(w):
            at = len(_words(" ".join(raw[:i + 1])))  # tokens up to and including the operator
            if any(_same_word(x, t) for x in words[at:at + _REACH] for t in terms):
                return True
    return False


def operator_problems(part: str, quotes: list[str], leads: list[str],
                      comparison: bool = True) -> list[str]:
    """Check 5 for one part: `quotes` are its excerpts as they stand in the cited text, `leads`
    the words just before each. A class is absent from a part when no word of it occurs there."""
    problems = []
    said = " \n ".join(quotes)
    for name, pattern in _BOTH_WAYS:
        in_part = bool(pattern.search(part))
        terms = [t for t in content_terms(part) if not pattern.search(t)]
        governing = any(pattern.search(x) for x in leads) or any(
            _governs(pattern, q, terms) for q in quotes)
        if in_part and not (pattern.search(said) or governing):
            problems.append(f"no excerpt states the {name} of part {part!r}")
        elif governing and not in_part:
            problems.append(f"an excerpt's {name} is missing from part {part!r}")
    for name, pattern in _STATED + ((("comparison", _COMPARISON),) if comparison else ()):
        if pattern.search(part) and not pattern.search(said):
            problems.append(f"no excerpt states the {name} of part {part!r}")
    return problems


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
    """Checks 1-5 of the module docstring on a model's SUPPORTED judgment of `text`."""
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
        if not p.excerpts:
            problems.append(f"part {p.part!r} has no excerpt")
        quotes, leads = [], []
        for e in p.excerpts:
            found = find_excerpt(e, text)
            if found is None:
                fabricated.append(e)
            else:
                spans.append(found)
                excerpts.append(e)
                quotes.append(text[found[0]:found[1]])
                leads.append(_lead(text, found))
        if quotes:
            problems += operator_problems(p.part, quotes, leads)
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
    """Checks 1-5 for a statement resting on several premises (ADR-0006 §6): every excerpt is in
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
        if not p.excerpts:
            problems.append(f"part {p.part!r} has no excerpt")
        quotes, leads = [], []
        for n, e in p.excerpts:
            found = find_excerpt(e, texts[n - 1]) if 1 <= n <= len(texts) else None
            if found is None:
                fabricated.append(e)
            else:
                spans.append(found)
                excerpts.append(e)
                quotes.append(texts[n - 1][found[0]:found[1]])
                leads.append(_lead(texts[n - 1], found))
        if quotes:
            problems += operator_problems(p.part, quotes, leads, comparison=False)
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
