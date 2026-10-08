from insightforge_agent.domain.models import Passage


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
