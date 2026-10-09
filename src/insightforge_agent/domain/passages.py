import re
from dataclasses import dataclass

from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.tokens import estimate_tokens

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def split_paragraphs(body: str) -> list[str]:
    """Split a web body into paragraphs on blank lines, dropping empty ones."""
    return [p.strip() for p in body.replace("\r\n", "\n").split("\n\n") if p.strip()]


def check_passages(observation_id: str, passages: list[Passage]) -> None:
    """Shared by every backend so they reject the same input."""
    if any(p.observation_id != observation_id for p in passages):
        raise ValueError(f"a passage does not belong to observation {observation_id}")
    indices = [p.index for p in passages]
    if len(indices) != len(set(indices)):
        raise ValueError(f"duplicate passage index in observation {observation_id}")


@dataclass(frozen=True)
class Window:
    """A bounded slice of one Passage's text, for one model call. The Passage keeps its stored
    identity and citation reference; a window is temporary and is never stored."""

    passage: Passage
    text: str
    part: int = 1
    parts: int = 1

    @property
    def shown(self) -> str:
        """The text as a prompt shows it, marking a partial Passage."""
        return self.text if self.parts == 1 else f"(part {self.part} of {self.parts}) {self.text}"


def _pieces(text: str, token_cap: int) -> list[str]:
    """Cut `text` into pieces of at most `token_cap` estimated tokens, at whitespace where one
    exists. Nothing is dropped except the whitespace a cut falls on."""
    limit = max(1, 4 * token_cap - 1)  # estimate_tokens(t) = len(t) // 4 + 1
    pieces, rest = [], text
    while estimate_tokens(rest) > token_cap:
        cut = rest.rfind(" ", 0, limit + 1)
        cut = cut if cut > 0 else limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    pieces.append(rest)
    return [p for p in pieces if p]


def windows_of(passage: Passage, token_cap: int) -> list[Window]:
    pieces = _pieces(passage.text, token_cap) or [passage.text]
    return [Window(passage, t, n, len(pieces)) for n, t in enumerate(pieces, start=1)]


def batch_passages(passages: list[Passage], token_cap: int) -> list[list[Window]]:
    """Group Passages, in order, so each group's text fits `token_cap`. A Passage that is over
    the cap on its own is read in several windows, never truncated and never re-split in the
    store: each window still names the original Passage."""
    batches: list[list[Window]] = []
    used = 0
    for window in (w for p in passages for w in windows_of(p, token_cap)):
        cost = estimate_tokens(window.text)
        if not batches or used + cost > token_cap:
            batches.append([])
            used = 0
        batches[-1].append(window)
        used += cost
    return batches


def value_in_text(value: str, text: str) -> bool:
    """Whether a structured value is stated in the text: its numbers appear in it, or, for a
    value with no number, the value itself does."""
    if not value.strip():
        return False
    numbers = numbers_in(value)
    if numbers:
        return numbers <= numbers_in(text)
    return value.strip().casefold() in text.casefold()


_SENTENCE_END = (".", "!", "?", ":")


def named_entities_in(text: str) -> set[str]:
    """Capitalised words that are not the first word of a sentence: a cheap, deterministic
    stand-in for the named entities of a statement. Possessives and punctuation are removed."""
    found: set[str] = set()
    previous = ""
    for raw in text.split():
        word = raw.strip("\"'()[],;:.!?")
        if word.endswith(("'s", "’s")):
            word = word[:-2]
        starts_sentence = not previous or previous.endswith(_SENTENCE_END)
        if not starts_sentence and len(word) > 1 and word[0].isupper():
            found.add(word)
        previous = raw
    return found
