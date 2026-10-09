import re

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


def batch_passages(passages: list[Passage], token_cap: int) -> list[list[Passage]]:
    """Group Passages, in order, so each group's text fits `token_cap`. A group is split,
    never truncated. One Passage is the smallest unit: a single Passage over the cap is a
    batch of its own, since cutting it would break its identity."""
    batches: list[list[Passage]] = []
    used = 0
    for p in passages:
        cost = estimate_tokens(p.text)
        if not batches or used + cost > token_cap:
            batches.append([])
            used = 0
        batches[-1].append(p)
        used += cost
    return batches
