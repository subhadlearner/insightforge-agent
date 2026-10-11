"""Text embeddings behind one `Embedder` interface (design.md section 10).

`FastEmbedEmbedder` is the production model: the local `fastembed` model named in
`Settings.embedding_model` (default `BAAI/bge-small-en-v1.5`), the same in every environment and
independent of the chat provider. `HashingEmbedder` is a dependency-free, deterministic
bag-of-words embedding used where tests need exact, offline vectors."""

import hashlib
import math
import re
import threading
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


class FastEmbedEmbedder:
    """A local ONNX model through `fastembed`. Loaded on first use; the first load downloads
    the model weights once into fastembed's cache (no API key, no per-call cost)."""

    def __init__(self, model_name: str, cache_dir: str | None = None) -> None:
        self.model_name = model_name
        self._cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()  # parallel Doc Readers must not load the model twice

    @property
    def dimensions(self) -> int:
        from fastembed import TextEmbedding

        for m in TextEmbedding.list_supported_models():
            if m["model"] == self.model_name:
                return int(m["dim"])
        raise ValueError(f"fastembed does not support {self.model_name!r}")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(self.model_name, cache_dir=self._cache_dir)
        return [v.tolist() for v in self._model.embed(texts)]


def cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0
