"""Which names an Evidence statement asserts, and whether a Passage states them (T18 CS1B).

Semantic-rules ledger row 1. The check guards against invented names; it is lexical and says
nothing about whether the Passage supports the statement (the entailment call decides that).

- Asserted names are the statement's own: its capitalised non-initial words
  (`named_entities_in`), each sentence's first word when it is name-like, and any model-listed
  entity the statement contains word for word. Each must be in the Passage.
- A model-listed entity the statement does not contain is a *label* (a classification such as
  "Nvidia H20 inventory write-down"). It can never add a name: only its words that the
  statement itself uses as names (anchors) must be in the Passage. A label with no anchor
  imposes nothing and is reported.
- Matching is by whole token or phrase, case-insensitive, after removing form differences only:
  possessives, dotted acronyms (U.S. = US), a plural "s", parts of hyphenated words
  (non-GAAP holds GAAP), and footnote numbers after a name ending in a letter (Tesla3) or after
  a dot (H20.14). Model numbers stay exact: H20 is not H200, A10 is not A100."""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from insightforge_agent.domain.passages import named_entities_in

# A sentence-initial word that names nothing: articles, determiners, pronouns, prepositions,
# conjunctions and discourse openers.
OPENERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "it", "its", "their", "they", "there",
    "in", "on", "at", "by", "for", "from", "with", "as", "after", "before", "during", "since",
    "until", "while", "when", "if", "although", "however", "overall", "according", "despite",
    "both", "each", "all", "some", "most", "many", "several", "about", "around", "over",
    "under", "between", "among", "of", "to", "and", "but", "or", "no", "not", "only", "such",
    "other", "one", "two", "three", "four", "five", "first", "second", "third", "last", "next",
    "new", "we", "he", "she", "his", "her", "our", "what", "which", "who", "where", "why",
    "how", "yes", "also", "still", "yet", "then", "thus", "meanwhile", "instead",
})
# Unit and measure abbreviations written in capitals; never names.
UNITS = frozenset({
    "MW", "GW", "TW", "KW", "MWH", "GWH", "TWH", "KWH", "MWDC", "GWDC", "MWAC", "GWAC",
    "USD", "EUR", "RMB", "CNY", "GBP", "JPY", "YOY", "QOQ", "CAGR",
})

_TOKEN = re.compile(r"[\w][\w.\-&]*", re.UNICODE)
_DOTTED = re.compile(r"(?<![A-Za-z])(?:[A-Z]\.)+[A-Z](?![A-Za-z])\.?")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_FOOTNOTE_AFTER_DOT = re.compile(r"\.\d+$")


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("’", "'")
    return _DOTTED.sub(lambda m: m.group(0).replace(".", ""), text)


def tokens(text: str) -> list[str]:
    """Whole tokens, possessives removed; a hyphenated word also yields its parts."""
    out = []
    for m in _TOKEN.finditer(_plain(text)):
        word = m.group(0).rstrip(".-")
        if word.endswith("'s"):
            word = word[:-2]
        if not word:
            continue
        out.append(word)
        if "-" in word:
            out += [part for part in word.split("-") if part]
    return out


def common_words(texts: Iterable[str]) -> frozenset[str]:
    """Every word written all in lower case somewhere in `texts`: in the Passages a Run read,
    "revenue" and "global" are, and "tesla" or "medtronic" are not."""
    return frozenset(w for t in texts for w in _TOKEN.findall(t) if w.islower())


def _name_like(word: str, common: frozenset[str]) -> bool:
    return (any(c.isdigit() for c in word) or any(c.isupper() for c in word[1:])
            or word.casefold() not in common)


def asserted_names(statement: str, common: frozenset[str]) -> set[str]:
    """The names the statement itself asserts (model entity fields are not used)."""
    names = set(named_entities_in(statement))
    for sentence in _SENTENCE.split(_plain(statement).strip()):
        words = tokens(sentence)
        first = words[0] if words else ""
        capitalised = first[:1].isupper() or any(c.isupper() for c in first[1:])  # iPhone
        if (len(first) > 1 and capitalised and first.casefold() not in OPENERS
                and _name_like(first, common)):
            names.add(first)
    return {n for n in names if n.upper() not in UNITS}


def _same(have: str, want: str) -> bool:
    have = _FOOTNOTE_AFTER_DOT.sub("", have)
    return (have == want or have == want + "s"
            # a footnote number glued to a plain word; never to a model number (B30A1 != B30A)
            or (want.isalpha() and have.startswith(want) and have[len(want):].isdigit()))


def _contains(haystack: list[str], phrase: str) -> bool:
    want = [w.casefold() for w in tokens(phrase)]
    n = len(want)
    return bool(want) and any(all(_same(haystack[i + k], want[k]) for k in range(n))
                              for i in range(len(haystack) - n + 1))


def label_anchors(label: str, statement: str) -> set[str]:
    """The label's words that the statement itself uses and that are name-like."""
    used = {w.casefold() for w in tokens(statement)}
    return {w for w in tokens(label)
            if w.casefold() in used and (w[0].isupper() or any(c.isdigit() for c in w))
            and w.upper() not in UNITS}


@dataclass(frozen=True)
class NameCheck:
    missing: list[str]
    labels_ignored: list[str] = field(default_factory=list)


def missing_names(*, statement: str, entities: list[str], entity: str, passage_text: str,
                  common: frozenset[str]) -> NameCheck:
    statement_tokens = [w.casefold() for w in tokens(statement)]
    required = asserted_names(statement, common)
    ignored = []
    for name in dict.fromkeys(n.strip() for n in [*entities, entity] if n.strip()):
        if _contains(statement_tokens, name):
            required.add(name)  # the statement names it: asserted
            continue
        anchors = label_anchors(name, statement)
        if anchors:
            required |= anchors
        else:
            ignored.append(name)
    # A word of a longer required name is checked by that name (stricter); not twice.
    phrases = [tokens(n) for n in required if len(tokens(n)) > 1]
    required = {n for n in required
                if len(tokens(n)) > 1 or not any(n in words for words in phrases)}
    passage_tokens = [w.casefold() for w in tokens(passage_text)]
    return NameCheck(missing=sorted(n for n in required if not _contains(passage_tokens, n)),
                     labels_ignored=ignored)


# Words that name a kind of document, never the company it speaks for.
_GENERIC_DOCUMENT_WORDS = frozenset({
    "annual", "report", "reports", "financial", "statement", "statements", "form", "fiscal",
    "year", "quarterly", "results", "review", "summary", "letter", "shareholders", "the", "of",
    "and", "interim", "half", "full", "integrated", "sustainability",
})
_FILENAME_WORDS = re.compile(r"[\s_.\-]+")


def _capitalised_runs(text: str) -> list[list[str]]:
    runs: list[list[str]] = []
    current: list[str] = []
    for word in tokens(text):
        if word[:1].isupper() and not any(c.isdigit() for c in word):
            current.append(word)
            continue
        if current:
            runs.append(current)
        current = []
    if current:
        runs.append(current)
    return runs


def document_entity(filename: str, chunk_texts: list[str]) -> str | None:
    """The company an uploaded document speaks for (T18 CS2), when the document itself
    establishes it: a run of capitalised words its own text uses in at least two chunks, every
    word of which is in the file name, and which is not only generic document words (so a title
    such as "Annual Report" never qualifies). The longest such run if it is the only one that
    long, else None: a one-chunk document never establishes one."""
    stem = {w.casefold() for w in _FILENAME_WORDS.split(filename) if w}
    seen: dict[tuple[str, ...], set[int]] = {}
    for n, text in enumerate(chunk_texts):
        for run in _capitalised_runs(text):
            for i in range(len(run)):
                for j in range(i + 1, len(run) + 1):
                    words = tuple(run[i:j])
                    if all(w.casefold() in stem for w in words) and not all(
                            w.casefold() in _GENERIC_DOCUMENT_WORDS for w in words):
                        seen.setdefault(words, set()).add(n)
    found = [w for w, chunks in seen.items() if len(chunks) >= 2]
    if not found:
        return None
    longest = max(len(w) for w in found)
    top = [w for w in found if len(w) == longest]
    return " ".join(top[0]) if len(top) == 1 else None


def _within(name: str, entity: str) -> bool:
    """Whether `name` is the entity or a run of its words ("Northwind", "Health")."""
    want = [w.casefold() for w in tokens(name)]
    have = [w.casefold() for w in tokens(entity)]
    return bool(want) and any(have[i:i + len(want)] == want
                              for i in range(len(have) - len(want) + 1))


def without_credited_names(statement: str, names: list[str]) -> str | None:
    """The statement with the document's own company names (already credited to it by
    `credited_by_document`) removed, for the entailment judge (T18 Stage C): "Northwind Health
    research and development spending was 71 million dollars" becomes "Research and development
    spending was 71 million dollars". Only those names go; every other word, number and
    qualifier stays, so the judge still decides the fact itself. None when nothing is removed
    or too little would remain to judge."""
    text = statement
    for name in sorted(set(names), key=len, reverse=True):
        words = r"\s+".join(re.escape(w) for w in name.split())
        text = re.sub(rf"\b{words}(?:'s|’s)?\b\s*", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", " ".join(text.split()))
    if text == " ".join(statement.split()) or len(tokens(text)) < 3:
        return None
    return text[:1].upper() + text[1:]


def credited_by_document(missing: list[str], *, entity: str | None, statement: str,
                         chunk_text: str, common: frozenset[str]) -> bool:
    """Whether the missing names are all the document's own entity, in a chunk of that same
    document that names no one else. A proper name in the chunk that the statement does not
    assert, that is not the entity and is not a code (NW-200) denies the credit, so a chunk
    about another company is never attributed to the document's company."""
    if not entity or not missing or not all(_within(m, entity) for m in missing):
        return False
    # Words the statement writes in lower case are common words here ("pricing pressure"), and
    # only the chunk sentences the statement draws on (most shared words) are checked.
    common = common | common_words([statement])
    stated = {w.casefold() for n in asserted_names(statement, common) for w in tokens(n)}
    words = {w.casefold() for w in tokens(statement) if w.casefold() not in OPENERS}
    sentences = _SENTENCE.split(_plain(chunk_text).strip())
    overlap = [len(words & {w.casefold() for w in tokens(s)}) for s in sentences]
    drawn_on = " ".join(s for s, n in zip(sentences, overlap, strict=True)
                        if n and n == max(overlap))
    for name in asserted_names(drawn_on, common):
        if any(c.isdigit() for c in name) or _within(name, entity):
            continue
        if not {w.casefold() for w in tokens(name)} <= stated:
            return False
    return True
