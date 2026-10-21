"""The `report_sections` Qdrant collection (design.md section 6): semantic search over the Claims
of a User's earlier Reports, for the Memory researcher.

A retrieval index only. One point per Claim, carrying `{owner_id, report_id, run_id,
section_index, claim_index, heading, report_date, embedding_model}`, and no text: the Claim and
its Passage lineage are always read back from the persisted Report, never from here. Every
search is filtered by `owner_id`, the embedding model and an explicit set of Report IDs (the
Reports the eligibility rule allows), inside this class. Nothing here deletes a point: Reports
are never deleted."""

import uuid
from dataclasses import dataclass
from datetime import date

from qdrant_client import QdrantClient, models

from insightforge_agent.domain.memory import MemoryRef
from insightforge_agent.embeddings import Embedder
from insightforge_agent.stores.vector_collections import ensure_collection

COLLECTION = "report_sections"
DEFAULT_K = 5
_NAMESPACE = uuid.UUID("2b7f0c4e-8d1a-4f6b-a3c2-5e9d7b1a0c44")


@dataclass(frozen=True)
class SectionEntry:
    """One Claim of a Report to index: its position, its section heading and its text."""

    section: int
    claim: int
    heading: str
    text: str


@dataclass(frozen=True)
class SectionHit:
    ref: MemoryRef
    run_id: str
    heading: str
    report_date: str
    relevance_score: float


class ReportSectionIndex:
    def __init__(self, client: QdrantClient, embedder: Embedder, model_name: str,
                 dimensions: int) -> None:
        self._client, self._embedder = client, embedder
        self._model, self._dimensions = model_name, dimensions
        self._ready = False

    def _ensure_collection(self) -> None:
        if not self._ready:
            ensure_collection(self._client, COLLECTION, self._dimensions, self._model,
                              ("owner_id", "report_id", "embedding_model"))
            self._ready = True

    @staticmethod
    def _point_id(owner_id: str, report_id: str, section: int, claim: int) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{owner_id}|{report_id}|{section}|{claim}"))

    def index(self, owner_id: str, report_id: str, run_id: str, report_date: date,
              entries: list[SectionEntry]) -> int:
        """Index a Report's Claims. Idempotent: indexing the same Report again rewrites the
        same points. Returns the number of Claims indexed."""
        self._ensure_collection()
        if not entries:
            return 0
        vectors = self._embedder.embed([f"{e.heading}: {e.text}" for e in entries])
        self._client.upsert(COLLECTION, points=[
            models.PointStruct(
                id=self._point_id(owner_id, report_id, e.section, e.claim), vector=list(v),
                payload={"owner_id": owner_id, "report_id": report_id, "run_id": run_id,
                         "section_index": e.section, "claim_index": e.claim,
                         "heading": e.heading, "report_date": report_date.isoformat(),
                         "embedding_model": self._model})
            for e, v in zip(entries, vectors, strict=True)])
        return len(entries)

    def search(self, owner_id: str, report_ids: list[str], query: str,
               k: int = DEFAULT_K) -> list[SectionHit]:
        """The `k` Claims most similar to the query, from this User's Reports in `report_ids`
        only. An empty set searches nothing."""
        if not report_ids:
            return []
        self._ensure_collection()
        must = [models.FieldCondition(key="owner_id", match=models.MatchValue(value=owner_id)),
                models.FieldCondition(key="embedding_model",
                                      match=models.MatchValue(value=self._model)),
                models.FieldCondition(key="report_id",
                                      match=models.MatchAny(any=sorted(set(report_ids))))]
        found = self._client.query_points(
            COLLECTION, query=list(self._embedder.embed([query])[0]),
            query_filter=models.Filter(must=must), limit=k, with_payload=True).points
        return [SectionHit(
            ref=MemoryRef(p.payload["report_id"], p.payload["section_index"],
                          p.payload["claim_index"]),
            run_id=p.payload["run_id"], heading=p.payload["heading"],
            report_date=p.payload["report_date"], relevance_score=round(p.score, 6))
            for p in found]
