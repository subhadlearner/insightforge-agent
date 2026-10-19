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
