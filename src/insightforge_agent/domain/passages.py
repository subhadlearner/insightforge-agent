import re
from collections.abc import Callable
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


Label = Callable[[int, Window], str]


def position_label(position: int, window: Window) -> str:
    return str(position)


def render_batch(batch: list[Window], label: Label = position_label) -> str:
    """The Passage portion of a model payload, exactly as it is sent: numbered labels, part
    markers and separators included. The per-call cap is measured on this string."""
    return "\n\n".join(f"[{label(n, w)}] {w.shown}" for n, w in enumerate(batch, start=1))


def _pieces(text: str, limit: int) -> list[str]:
    """Cut `text` into pieces of at most `limit` characters, at whitespace where one exists.
    Nothing is dropped except the whitespace a cut falls on."""
    pieces, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit + 1)
        cut = cut if cut > 0 else limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    pieces.append(rest)
    return [p for p in pieces if p]


def _windows_for(passage: Passage, token_cap: int, label: Label) -> list[Window]:
    """The Passage whole if it fits a batch of its own as rendered, else the fewest bounded
    windows that each do."""
    def fits(w: Window) -> bool:
        return estimate_tokens(render_batch([w], label)) <= token_cap

    whole = Window(passage, passage.text)
    if fits(whole):
        return [whole]
    limit = max(1, 4 * token_cap - 1)
    while True:
        pieces = _pieces(passage.text, limit) or [passage.text]
        windows = [Window(passage, t, n, len(pieces)) for n, t in enumerate(pieces, start=1)]
        if limit <= 1 or all(fits(w) for w in windows):
            return windows
        limit -= 1


def batch_passages(
    passages: list[Passage], token_cap: int, label: Label = position_label,
) -> list[list[Window]]:
    """Group Passages, in order, so each batch's rendered payload (`render_batch`) fits
    `token_cap`. A Passage that cannot fit on its own is read in several windows, never
    truncated and never re-split in the store: each window still names the original Passage."""
    batches: list[list[Window]] = []
    for passage in passages:
        for window in _windows_for(passage, token_cap, label):
            if batches and estimate_tokens(render_batch(batches[-1] + [window], label)) <= token_cap:
                batches[-1].append(window)
            else:
                batches.append([window])
    return batches


_SPACE_CURRENCY = re.compile(r"([$€£])\s*(\d[\d,]*(?:\.\d+)?)")
_CURRENCY = {"$": "dollar", "€": "euro", "£": "pound"}
_VALUE_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|[^\W\d_]+|%")
_ALIASES = {"percentage": "percent", "pct": "percent", "usd": "dollar", "eur": "euro",
            "gbp": "pound", "per": "per"}


def _value_tokens(text: str) -> list[str]:
    """Numbers (without separators) and words, with a currency or percent sign read as the unit
    word that follows its number, so "$32,000" and "32,000 dollars" are the same value."""
    text = _SPACE_CURRENCY.sub(lambda m: f"{m.group(2)} {_CURRENCY[m.group(1)]}", text)
    text = re.sub(r"per\s+cent", "percent", text, flags=re.IGNORECASE)
    tokens = []
    for raw in _VALUE_TOKEN.findall(text):
        if raw[0].isdigit():
            tokens.append(raw.replace(",", "").rstrip("."))
        else:
            word = "percent" if raw == "%" else raw.casefold()
            word = _ALIASES.get(word, word)
            tokens.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return tokens


def value_in_text(value: str, text: str) -> bool:
    """Whether a structured value is stated in the text: its numbers and units (million vs
    billion, percent, currency) appear in it together, in order, as one run of words. Deliberately
    conservative: an abbreviation such as "4.27m" is not matched with "4.27 million"."""
    wanted = _value_tokens(value)
    if not wanted:
        return False
    have = _value_tokens(text)
    return any(have[i:i + len(wanted)] == wanted for i in range(len(have) - len(wanted) + 1))


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
