"""INGESTING: uploaded PDFs become Sources, Passages and `doc_chunks` entries (design.md section 5).

Runs only when documents were uploaded. Each PDF is chunked once (Docling), stored through
`SourceStore` (the Source ID is derived from the file's content hash, each chunk is a Passage
with its page and section heading) and indexed for the Doc Reader. A document that cannot be
read is skipped and logged; it never fails the other documents. No model is called."""

from insightforge_agent.agents.doc_reader import UPLOADED_DOCUMENT_CREDIBILITY, ChunkingError
from insightforge_agent.domain.documents import Upload
from insightforge_agent.pipeline.deps import Deps


def ingest_documents(deps: Deps, owner_id: str, run_id: str, uploads: list[Upload]) -> list[str]:
    """Ingest the Run's uploads. Returns the Source IDs of the documents that were read."""
    ingested: list[str] = []
    for upload in {u.content_hash: u for u in uploads}.values():  # the same file once
        try:
            drafts = deps.chunker.chunk(upload.filename, upload.data)
        except ChunkingError as e:
            deps.log(owner_id, run_id, "document_unreadable", filename=upload.filename,
                     document_id=upload.document_id, reason=str(e)[:500])
            continue
        obs = deps.store.ingest_document(
            owner_id=owner_id, run_id=run_id, content_hash=upload.content_hash,
            filename=upload.filename, chunks=drafts, fetched_at=deps.now(),
            credibility_score=UPLOADED_DOCUMENT_CREDIBILITY)
        contents = deps.store.read_source(owner_id, obs.source_id)
        indexed = deps.doc_index.index(owner_id, run_id, contents.source, contents.passages)
        deps.log(owner_id, run_id, "document_ingested", filename=upload.filename,
                 document_id=upload.document_id, source_id=obs.source_id,
                 observation_id=obs.id, chunks=indexed,
                 pages=len({p.page for p in contents.passages if p.page is not None}))
        ingested.append(obs.source_id)
    return ingested
