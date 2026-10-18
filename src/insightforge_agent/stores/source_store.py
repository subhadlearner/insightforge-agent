"""SourceStore: the one door to Sources, Observations and Passage text (design.md section 4)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from insightforge_agent.domain.contracts import Citation, PassageRef
from insightforge_agent.domain.errors import NotFoundError
from insightforge_agent.domain.ids import derive_observation_id, derive_source_id
from insightforge_agent.domain.models import Observation, Passage, Source, SourceKind
from insightforge_agent.domain.passages import split_paragraphs
from insightforge_agent.domain.repositories import (
    ObservationRepository,
    PassageRepository,
    SourceRepository,
)


@dataclass(frozen=True)
class PassageDraft:
    """A pre-split Passage, for material already chunked (an uploaded PDF)."""

    text: str
    page: int | None = None
    section_heading: str | None = None


@dataclass(frozen=True)
class SourceContents:
    """What `read_source` resolves: the Source, its latest Observation (of one Run, for
    `read_run_source`) and that Observation's Passages (empty when there is none)."""

    source: Source
    observation: Observation | None
    passages: list[Passage]


class SourceStore:
    def __init__(
        self,
        sources: SourceRepository,
        observations: ObservationRepository,
        passages: PassageRepository,
    ) -> None:
        self._sources = sources
        self._observations = observations
        self._passages = passages

    def ingest_web(
        self,
        *,
        owner_id: str,
        run_id: str,
        url: str,
        title: str,
        body: str,
        fetched_at: datetime,
        credibility_score: float | None = None,
        published_at: datetime | None = None,
    ) -> Observation:
        """Store one fetch of a web page, split into paragraph Passages."""
        return self._ingest(
            owner_id=owner_id, run_id=run_id, kind="web", locator=url, title=title,
            drafts=lambda: [PassageDraft(t) for t in split_paragraphs(body)],
            content=body, fetched_at=fetched_at, credibility_score=credibility_score,
            published_at=published_at,
        )

    def ingest_document(
        self,
        *,
        owner_id: str,
        run_id: str,
        content_hash: str,
        filename: str,
        chunks: list[PassageDraft],
        fetched_at: datetime,
        credibility_score: float | None = None,
        published_at: datetime | None = None,
    ) -> Observation:
        """Store an uploaded PDF's chunks. The Source is identified by the file's hash."""
        return self._ingest(
            owner_id=owner_id, run_id=run_id, kind="pdf", locator=content_hash, title=filename,
            drafts=lambda: chunks, content="\n\n".join(c.text for c in chunks),
            fetched_at=fetched_at, credibility_score=credibility_score,
            published_at=published_at,
        )

    def _ingest(
        self, *, owner_id: str, run_id: str, kind: SourceKind, locator: str, title: str,
        drafts: Callable[[], list[PassageDraft]], content: str, fetched_at: datetime,
        credibility_score: float | None, published_at: datetime | None,
    ) -> Observation:
        sid = derive_source_id(kind, locator)
        self._sources.add(Source(
            id=sid, owner_id=owner_id, run_id=run_id, kind=kind, locator=locator, title=title,
            credibility_score=credibility_score, published_at=published_at,
        ))
        oid = derive_observation_id(owner_id, run_id, sid, fetched_at, content)
        try:
            # Already stored (even if it split to nothing): never re-split.
            return self._observations.get(owner_id, oid)
        except NotFoundError:
            pass
        # Passages and their split marker go first, in one atomic write; the Observation
        # row goes last. A crash between the two leaves only a finished split with no
        # Observation, and a retry keeps that split and adds the Observation. An Observation
        # that exists therefore always has its Passages.
        self._passages.add_all(owner_id, oid, [
            Passage(observation_id=oid, index=i, text=d.text, page=d.page,
                    section_heading=d.section_heading)
            for i, d in enumerate(drafts())
        ])
        return self._observations.add(Observation(
            id=oid, owner_id=owner_id, source_id=sid, run_id=run_id, fetched_at=fetched_at,
        ))

    def read_source(self, owner_id: str, source_id: str) -> SourceContents:
        """Resolve a Source ID to the User's latest Observation of it, from whichever Run.
        Raises NotFoundError if it is missing or not the User's. A pipeline stage reads its
        own Run's fetch instead (`read_run_source`)."""
        source = self._sources.get(owner_id, source_id)
        return self._contents(owner_id, source,
                              self._observations.list_for_source(owner_id, source_id))

    def read_run_source(self, owner_id: str, run_id: str, source_id: str) -> SourceContents:
        """Resolve a Source ID to the latest Observation *this Run* made of it. A web Source is
        shared by every Run that fetched its URL, so the User's latest Observation may be
        another Run's (concurrent Runs); a Run must never read that. The observation is None
        if this Run never fetched the Source. Raises NotFoundError as `read_source`."""
        source = self._sources.get(owner_id, source_id)
        return self._contents(owner_id, source, [
            o for o in self._observations.list_for_source(owner_id, source_id)
            if o.run_id == run_id])

    def _contents(self, owner_id: str, source: Source,
                  observations: list[Observation]) -> SourceContents:
        latest = observations[-1] if observations else None
        passages = self._passages.list(owner_id, latest.id) if latest else []
        return SourceContents(source=source, observation=latest, passages=passages)

    def get_passages(self, owner_id: str, observation_id: str, indices: list[int]) -> list[Passage]:
        """A bounded batch of Passages by identity, in the order asked."""
        return [self._passages.get(owner_id, observation_id, i) for i in indices]

    def source_for_passage(self, owner_id: str, ref: PassageRef) -> Source:
        """Resolve only Source metadata behind a Passage reference, without reading text."""
        observation = self._observations.get(owner_id, ref.observation_id)
        # Validate that the cited Passage still exists; only its Source metadata is returned.
        # This keeps export/share provenance failure semantics aligned with the citation API
        # without exposing Passage text in the public DTO.
        self._passages.get(owner_id, ref.observation_id, ref.index)
        return self._sources.get(owner_id, observation.source_id)

    def citation(self, owner_id: str, ref: PassageRef) -> Citation:
        """Resolve a Passage reference to its text and Source. Claim position and Evidence id
        are left for the caller to fill. Raises NotFoundError if any record is missing."""
        passage = self._passages.get(owner_id, ref.observation_id, ref.index)
        source = self._sources.get(
            owner_id, self._observations.get(owner_id, ref.observation_id).source_id)
        return Citation(
            section=0, claim=0, evidence_id="", source_id=source.id, source_title=source.title,
            source_domain=source.domain,
            published_at=source.published_at, credibility_score=source.credibility_score,
            passage=ref, passage_text=passage.text, page=passage.page,
            section_heading=passage.section_heading)
