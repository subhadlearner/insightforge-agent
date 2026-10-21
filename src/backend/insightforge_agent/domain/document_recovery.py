"""Recovering a documents Sub-task whose Document Reader failed only because its search tool
shows chunk metadata, never text (T18 CS2; semantic-rules ledger row 21).

The Document Reader picks chunks from `doc_search` results that carry ids, pages and scores but
no text (agents/doc_reader.py). When it then replies `FAILED:` because that is all it was given,
its own searches still returned chunks: those may be recovered as *candidates* for Synthesis,
which checks provenance, figures and entailment as for any chunk.

Only that failure is recovered. The reply must say the search gave only metadata, or that chunk
text was not returned (lexicon DOC_READER_LIMITATION_V2, closed and versioned); a reply that
also reports a tool or access error, or makes any relevance judgment, is never recovered, nor is
"cannot verify" or "cannot confirm" on its own, an empty or missing search, or anything not
starting `FAILED:`."""

import re

LIMITATION_LEXICON = "DOC_READER_LIMITATION_V2"
# The metadata-only limitation, stated explicitly: only metadata came back, or no chunk text did.
_LIMITATION = re.compile(
    r"\b(?:(?:returned|returns|gave|gives|showed|shows|provided|provides)\s+only\s+"
    r"(?:chunk\s+)?metadata"
    r"|only\s+(?:chunk\s+)?metadata|metadata[- ]only"
    r"|no\s+(?:chunk\s+)?text\s+content|no\s+chunk\s+text|without\s+(?:any\s+)?(?:chunk\s+)?text"
    r"|(?:chunk\s+)?(?:text|content)\s+(?:is|was|were|are)\s+not\s+"
    r"(?:returned|included|shown|provided|available))\b", re.IGNORECASE)
# Anything else that went wrong: the failure is then not (only) the limitation.
_TOOL_ERROR = re.compile(
    r"\b(?:error|errors|exception|timed\s*out|timeout|unauthori[sz]ed|not\s+authori[sz]ed|"
    r"forbidden|permission|access\s+denied|rate[- ]?limit\w*|crash\w*|failed\s+to\s+"
    r"(?:run|call|execute|search|load|open))\b", re.IGNORECASE)
# A relevance judgment: the Reader's own decision, respected even beside the limitation.
_IRRELEVANT = re.compile(
    r"\b(?:not\s+relevant|irrelevant|unrelated|(?:does|do)\s+not\s+cover|doesn't\s+cover|"
    r"don't\s+cover|no\s+relevant|nothing\s+relevant|off[- ]topic)\b", re.IGNORECASE)
FAILED_PREFIX = "FAILED:"
RECOVERY_CAP = 16  # two searches of k = 8: the Document Reader may search once more


def recoverable(reply: str, searches: list[list[str]]) -> tuple[bool, list[str]]:
    """Whether a Document Reader's reply is the metadata-only failure and nothing else, given the
    chunk ids each of its own searches returned (in order). Returns the matched phrases."""
    text = (reply or "").strip()
    if not text.startswith(FAILED_PREFIX) or not searches or not all(searches):
        return False, []
    if _TOOL_ERROR.search(text) or _IRRELEVANT.search(text):
        return False, []
    phrases = list(dict.fromkeys(m.group(0).lower() for m in _LIMITATION.finditer(text)))
    return bool(phrases), phrases


def irrelevance_stated(reply: str) -> bool:
    """For the log: whether the reply made an explicit relevance judgment."""
    return bool(_IRRELEVANT.search(reply or ""))


def recovered_chunks(searches: list[list[str]], cap: int = RECOVERY_CAP) -> list[str]:
    """The Sub-task's own search results, best rank first (then earlier search, then first
    appearance), each once, at most `cap`."""
    best: dict[str, tuple[int, int]] = {}
    for n, ids in enumerate(searches):
        for rank, chunk in enumerate(ids):
            best[chunk] = min(best.get(chunk, (rank, n)), (rank, n))
    return sorted(best, key=lambda c: best[c])[:cap]
