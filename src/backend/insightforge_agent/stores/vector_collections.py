"""Shared Qdrant collection setup for `doc_chunks` and `report_sections` (design.md section 6)."""

import warnings

from qdrant_client import QdrantClient, models


def ensure_collection(client: QdrantClient, name: str, dimensions: int, model_name: str,
                      keyword_fields: tuple[str, ...]) -> None:
    """Create the collection with keyword payload indexes, or check that an existing one holds
    vectors of the embedding model's size. The model name is recorded on every point rather
    than in collection metadata, which the pinned Qdrant server (v1.12) does not support."""
    if not client.collection_exists(name):
        try:
            client.create_collection(name, vectors_config=models.VectorParams(
                size=dimensions, distance=models.Distance.COSINE))
        except Exception:
            if not client.collection_exists(name):
                raise  # a real failure, not another process creating it first
        with warnings.catch_warnings():  # in-memory Qdrant (tests) ignores payload indexes
            warnings.simplefilter("ignore", UserWarning)
            for field in keyword_fields:
                client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
    else:
        size = client.get_collection(name).config.params.vectors.size
        if size != dimensions:
            raise RuntimeError(
                f"{name} holds {size}-dimensional vectors, but {model_name} gives "
                f"{dimensions}; use a separate Qdrant collection or a fresh volume")
