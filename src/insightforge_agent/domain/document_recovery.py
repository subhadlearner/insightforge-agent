"""Recovering a documents Sub-task whose Document Reader failed only because its search tool
shows chunk metadata, never text (T18 CS2; semantic-rules ledger row 21).

The Document Reader picks chunks from `doc_search` results that carry ids, pages and scores but
no text (agents/doc_reader.py). When it then replies `FAILED:` because it could not read them,
its own searches still returned chunks: those may be recovered as *candidates* for Synthesis,
which checks provenance, figures and entailment as for any chunk. Nothing else is recovered:
an empty or missing search, any other reason, or an explicit relevance judgment that does not
blame missing text keeps the Sub-task failed. The lexicon is closed and versioned."""

import re

LIMITATION_LEXICON = "DOC_READER_LIMITATION_V1"
# Phrases by which a Document Reader says it could not read the chunks it was given.
_LIMITATION = re.compile(
    r"\b(metadata|no text|without text|text content|"
    r"(?:could not|couldn't|cannot|can't|can not|unable to) (?:read|confirm|verify)|"
    r"needs? to be read)\b", re.IGNORECASE)
# An explicit relevance judgment: respected unless the reply also blames missing text.
_IRRELEVANT = re.compile(r"\b(not relevant|unrelated|does not cover|no relevant|off-topic)\b",
                         re.IGNORECASE)
FAILED_PREFIX = "FAILED:"
RECOVERY_CAP = 16  # two searches of k = 8: the Document Reader may search once more


def recoverable(reply: str, searches: list[list[str]]) -> tuple[bool, list[str]]:
    """Whether a Document Reader's reply is the recognised metadata-only failure, given the
    chunk ids each of its own searches returned (in order). Returns the matched phrases; a
    relevance judgment alone matches none, so it is never recovered."""
    text = (reply or "").strip()
    if not text.startswith(FAILED_PREFIX) or not searches or not all(searches):
        return False, []
    phrases = list(dict.fromkeys(m.group(0).lower() for m in _LIMITATION.finditer(text)))
    return bool(phrases), phrases


def irrelevance_stated(reply: str) -> bool:
    """For the log: whether the reply also made an explicit relevance judgment."""
    return bool(_IRRELEVANT.search(reply or ""))


def recovered_chunks(searches: list[list[str]], cap: int = RECOVERY_CAP) -> list[str]:
    """The Sub-task's own search results, best rank first (then earlier search, then first
    appearance), each once, at most `cap`."""
    best: dict[str, tuple[int, int]] = {}
    for n, ids in enumerate(searches):
        for rank, chunk in enumerate(ids):
            best[chunk] = min(best.get(chunk, (rank, n)), (rank, n))
    return sorted(best, key=lambda c: best[c])[:cap]
