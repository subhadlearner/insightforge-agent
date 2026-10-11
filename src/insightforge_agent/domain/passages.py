import re
from collections.abc import Callable
from dataclasses import dataclass

from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.tokens import estimate_tokens

from insightforge_agent.domain.quantities import (  # noqa: F401 - numbers_in is re-exported
    numbers_in,
    parse_value,
    quantities_in,
    stated_by,
)


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


MAX_SENTENCE_CHARS = 600  # a longer "sentence" (scraped text without stops) is cut into pieces

# A word ending a sentence: "." "!" or "?", optionally followed by closing quotes or brackets.
_STOP = re.compile(r"[.!?][\"'”’)\]]*$")
# A short capitalised abbreviation ("Co.", "Ltd.", "Inc.", "Dr.") or dotted initials ("U.S.").
# A stop after one does not end a sentence, so a company name is not cut in two. Orthographic
# only: a capitalised word of at most four letters, or two or more single letters with stops.
_ABBREVIATION = re.compile(r"^(?:[A-Z][a-z]{0,3}|(?:[A-Za-z]\.)+[A-Za-z])\.$")


def sentences_of(text: str) -> list[str]:
    """The sentences of `text`, in order, each at most `MAX_SENTENCE_CHARS` long. A sentence
    ends at a stop followed by a word that does not start in lower case, unless the stop
    belongs to a short abbreviation. Deterministic; nothing but whitespace is dropped."""
    sentences, current = [], []
    words = text.split()
    for n, word in enumerate(words):
        current.append(word)
        following = words[n + 1] if n + 1 < len(words) else ""
        if (not following or (_STOP.search(word) and not following[0].islower()
                              and not _ABBREVIATION.match(word))):
            sentences.extend(_pieces(" ".join(current), MAX_SENTENCE_CHARS))
            current = []
    return sentences


@dataclass(frozen=True)
class Span:
    """A sentence, or two adjacent sentences, of one Passage: what the Fact-Checker compares a
    Claim with. Temporary like a Window; it still names, and is cited as, its parent Passage."""

    passage: Passage
    text: str


MAX_WINDOW_SENTENCES = 4  # the widest window, however many sentences a Claim has


def window_width(statement: str) -> int:
    """Windows as wide as the statement, so a Claim of several sentences can match the
    Passage text that states it: at least 2 sentences, at most `MAX_WINDOW_SENTENCES`."""
    return min(MAX_WINDOW_SENTENCES, max(2, len(sentences_of(statement))))


def candidate_spans(passages: list[Passage], width: int = 2) -> list[Span]:
    """Every sentence of every Passage, each followed by its windows with the next sentences
    (2 up to `width` sentences in all), so a statement split across a sentence boundary can
    still be matched. A Passage of n sentences gives fewer than n * width spans, each at most
    `width` bounded sentences, in a fixed order."""
    width = max(1, min(width, MAX_WINDOW_SENTENCES))
    spans = []
    for passage in passages:
        sentences = sentences_of(passage.text)
        for n in range(len(sentences)):
            for k in range(1, width + 1):
                if n + k <= len(sentences):
                    spans.append(Span(passage, " ".join(sentences[n:n + k])))
    return spans


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
    if any(have[i:i + len(wanted)] == wanted for i in range(len(have) - len(wanted) + 1)):
        return True
    quantity = parse_value(value)  # another representation of the same exact quantity
    return quantity is not None and any(stated_by(quantity, p) for p in quantities_in(text))


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
