"""Uploaded documents (design.md sections 4 and 5, Ingestion): limits, identity and chunk IDs.

Pure rules, no I/O. A chunk of an uploaded PDF is a Passage, so its ID is the Passage identity
`(observation_id, index)` written as one token the Doc Reader can hand back."""

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

from insightforge_agent.domain.contracts import Brief, PassageRef
from insightforge_agent.domain.errors import SubmissionRejected

MAX_DOCUMENTS = 10
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
PDF_SIGNATURE = b"%PDF-"

_CHUNK_ID = re.compile(r"\b(obs_[0-9a-f]{16}):(\d+)\b")


class Upload(BaseModel):
    """One uploaded PDF as received. Its bytes never enter graph state or a model call."""

    model_config = ConfigDict(frozen=True)

    filename: str = Field(min_length=1)
    data: bytes

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def document_id(self) -> str:
        """What a Brief lists in `document_ids`: derived from the content, like the Source ID."""
        return f"doc_{self.content_hash[:16]}"


def check_uploads(brief: Brief, uploads: list[Upload]) -> None:
    """At most 10 PDFs of at most 20 MB each, and exactly the documents the Brief names.
    Raises SubmissionRejected naming every problem; nothing is ingested then."""
    problems: list[str] = []
    if len(uploads) > MAX_DOCUMENTS:
        problems.append(f"{len(uploads)} documents uploaded; at most {MAX_DOCUMENTS} are allowed")
    for u in uploads:
        if len(u.data) > MAX_DOCUMENT_BYTES:
            problems.append(f"{u.filename} is larger than 20 MB")
        if not u.data.startswith(PDF_SIGNATURE):
            problems.append(f"{u.filename} is not a PDF")
    if sorted(set(brief.document_ids)) != sorted({u.document_id for u in uploads}):
        problems.append("the Brief's document_ids do not match the uploaded documents")
    if problems:
        raise SubmissionRejected("; ".join(problems))


def chunk_id(ref: PassageRef) -> str:
    return f"{ref.observation_id}:{ref.index}"


def chunk_ids_in(text: str) -> list[PassageRef]:
    """Chunk IDs named in a reply, in order, without repeats."""
    return list(dict.fromkeys(
        PassageRef(observation_id=o, index=int(i)) for o, i in _CHUNK_ID.findall(text)))
