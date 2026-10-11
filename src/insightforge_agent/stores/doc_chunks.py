"""The `doc_chunks` Qdrant collection (design.md section 6): semantic search over uploaded PDF chunks.

Every chunk is a Passage already stored through `SourceStore`, which stays the one door to its
text. Qdrant holds each chunk's vector with `{run_id, owner_id, source_filename, page,
section_heading}` plus its Passage identity, the chunk text, and the embedding model's name.
`search` returns chunk identities and scores only, never text. Every query is filtered by
`owner_id`, `run_id` (a Run searches only its own uploads) and the embedding model, inside this
class. Nothing here deletes a chunk: removal belongs to the retention sweep (T13), which must
keep every chunk a Report cites."""

import uuid
import warnings
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from insightforge_agent.domain.contracts import PassageRef
from insightforge_agent.domain.models import Passage, Source
from insightforge_agent.embeddings import Embedder

COLLECTION = "doc_chunks"
DEFAULT_K = 8
_NAMESPACE = uuid.UUID("6f1c2a52-3c1e-4d0b-9a35-0d6c1f0e7a11")


@dataclass(frozen=True)
class ChunkHit:
    passage: PassageRef
    source_id: str
    source_filename: str
    page: int | None
    relevance_score: float


class DocChunkIndex:
    def __init__(self, client: QdrantClient, embedder: Embedder, model_name: str,
                 dimensions: int) -> None:
        self._client, self._embedder = client, embedder
        self._model, self._dimensions = model_name, dimensions
        self._ready = False

    def _ensure_collection(self) -> None:
        if self._ready:
            return
        if not self._client.collection_exists(COLLECTION):
            try:
                self._client.create_collection(COLLECTION, vectors_config=models.VectorParams(
                    size=self._dimensions, distance=models.Distance.COSINE))
            except Exception:
                if not self._client.collection_exists(COLLECTION):
                    raise  # a real failure, not another process creating it first
            with warnings.catch_warnings():  # in-memory Qdrant (tests) ignores payload indexes
                warnings.simplefilter("ignore", UserWarning)
                for field in ("owner_id", "run_id", "embedding_model"):
                    self._client.create_payload_index(
                        COLLECTION, field, models.PayloadSchemaType.KEYWORD)
        else:
            size = self._client.get_collection(COLLECTION).config.params.vectors.size
            if size != self._dimensions:
                raise RuntimeError(
                    f"{COLLECTION} holds {size}-dimensional vectors, but {self._model} gives "
                    f"{self._dimensions}; use a separate Qdrant collection or a fresh volume")
        self._ready = True

    @staticmethod
    def _point_id(owner_id: str, run_id: str, ref: PassageRef) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{owner_id}|{run_id}|{ref.observation_id}|{ref.index}"))

    def index(self, owner_id: str, run_id: str, source: Source, passages: list[Passage],
              filename: str | None = None) -> int:
        """Index a document's chunks for one Run. Idempotent: the same chunk for the same Run
        keeps one point. `filename` is the name this Run uploaded it under (a Source keyed by
        content keeps the title of its first upload). Returns the number of chunks indexed."""
        self._ensure_collection()
        if not passages:
            return 0
        vectors = self._embedder.embed([p.text for p in passages])
        self._client.upsert(COLLECTION, points=[
            models.PointStruct(
                id=self._point_id(owner_id, run_id, PassageRef(observation_id=p.observation_id,
                                                               index=p.index)),
                vector=list(v),
                payload={"owner_id": owner_id, "run_id": run_id, "source_id": source.id,
                         "source_filename": filename or source.title, "page": p.page,
                         "section_heading": p.section_heading, "observation_id": p.observation_id,
                         "passage_index": p.index, "text": p.text,
                         "embedding_model": self._model})
            for p, v in zip(passages, vectors, strict=True)])
        return len(passages)

    def search(self, owner_id: str, run_id: str, query: str, k: int = DEFAULT_K) -> list[ChunkHit]:
        """The `k` chunks of this User's uploads for this Run most similar to the query."""
        self._ensure_collection()
        must = [models.FieldCondition(key=key, match=models.MatchValue(value=value))
                for key, value in (("owner_id", owner_id), ("run_id", run_id),
                                   ("embedding_model", self._model))]
        found = self._client.query_points(
            COLLECTION, query=list(self._embedder.embed([query])[0]),
            query_filter=models.Filter(must=must), limit=k,
            with_payload=["source_id", "source_filename", "page", "observation_id",
                          "passage_index"]).points
        return [ChunkHit(
            passage=PassageRef(observation_id=p.payload["observation_id"],
                               index=p.payload["passage_index"]),
            source_id=p.payload["source_id"], source_filename=p.payload["source_filename"],
            page=p.payload["page"], relevance_score=round(p.score, 6)) for p in found]
