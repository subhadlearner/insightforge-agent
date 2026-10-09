"""Text embeddings for relevance ranking (cosine similarity between an Evidence item and the Brief).

`HashingEmbedder` is a dependency-free, deterministic bag-of-words embedding: the same text
always gives the same vector in every environment. The local `fastembed` model named in
`Settings.embedding_model` arrives with the Qdrant-backed tickets (T7, T8) behind this same
`Embedder` interface, so ranking code does not change."""

import hashlib
import math
import re
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    def __init__(self, dimensions: int = 256) -> None:
        self._dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for word in _WORD.findall(text.lower()):
            slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
            vector[slot % self._dimensions] += 1.0
        return vector


def cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0
