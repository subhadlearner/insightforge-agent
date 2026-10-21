"""The sample brief library (`samples/briefs/*.yml`): each file is one Brief, the industry and
question type it covers, and the Report structure expected of it. A brief with memory on names
a `setup` Brief that runs first for the same User, so its Report is eligible memory."""

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload
from insightforge_agent.domain.plan import SourceType

LIBRARY_SIZE = 5
FIRST_HEADING = "Executive Summary"
LAST_HEADING = "Gaps & limitations"


class LibraryError(ValueError):
    """The library or one of its briefs is malformed; names the file."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpectedSection(_Strict):
    heading: str = Field(min_length=1)
    notes: str = ""
    source_types: list[SourceType] = Field(default_factory=list)
    must_cite: list[str] = Field(default_factory=list)


class ExpectedStructure(_Strict):
    sections: list[ExpectedSection]
    checks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _framed(self) -> "ExpectedStructure":
        headings = [s.heading for s in self.sections]
        if not headings or headings[0] != FIRST_HEADING:
            raise ValueError(f"the first expected section must be {FIRST_HEADING!r}")
        if headings[-1] != LAST_HEADING:
            raise ValueError(f"the last expected section must be {LAST_HEADING!r}")
        return self


class SetupRun(_Strict):
    runs_before: int = Field(ge=1)
    brief: str = Field(min_length=1)
    source_toggles: dict[SourceType, bool]
    expect: str = ""

    @model_validator(mode="after")
    def _no_memory(self) -> "SetupRun":
        if self.source_toggles.get("memory") or self.source_toggles.get("documents"):
            raise ValueError("a setup Run uses web only: it creates the memory, never reads it")
        return self

    def to_brief(self) -> Brief:
        return Brief(text=self.brief, source_toggles=self.source_toggles)


class SampleBrief(_Strict):
    id: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    question_type: str = Field(min_length=1)
    brief: str = Field(min_length=1)
    source_toggles: dict[SourceType, bool]
    documents: list[str] = Field(default_factory=list)  # paths relative to the repository root
    setup: SetupRun | None = None
    expected_report_structure: ExpectedStructure

    @model_validator(mode="after")
    def _sources_match(self) -> "SampleBrief":
        if bool(self.documents) != bool(self.source_toggles.get("documents")):
            raise ValueError("documents must be listed exactly when the documents toggle is on")
        if bool(self.setup) != bool(self.source_toggles.get("memory")):
            raise ValueError("a setup Run must be given exactly when the memory toggle is on")
        return self

    def uploads(self, root: Path) -> list[Upload]:
        return [Upload(filename=Path(p).name, data=(root / p).read_bytes())
                for p in self.documents]

    def to_brief(self, uploads: list[Upload]) -> Brief:
        return Brief(text=self.brief, source_toggles=self.source_toggles,
                     document_ids=[u.document_id for u in uploads])


def load_library(directory: Path, root: Path,
                 size: int | None = LIBRARY_SIZE) -> list[SampleBrief]:
    """Every `*.yml` brief in `directory`, by file name. `root` resolves document paths.
    `size=None` accepts any non-empty library (the eval report records how many it held)."""
    paths = sorted(directory.glob("*.yml"))
    if (size is not None and len(paths) != size) or not paths:
        raise LibraryError(f"{directory}: expected {size or 'at least 1'} sample briefs, "
                           f"found {len(paths)}")
    samples, seen = [], set()
    for path in paths:
        try:
            sample = SampleBrief.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValidationError, yaml.YAMLError) as e:
            raise LibraryError(f"{path.name}: {e}") from e
        if sample.id in seen:
            raise LibraryError(f"{path.name}: duplicate sample id {sample.id!r}")
        missing = [p for p in sample.documents if not (root / p).is_file()]
        if missing:
            raise LibraryError(f"{path.name}: documents not found: {', '.join(missing)}")
        seen.add(sample.id)
        samples.append(sample)
    return samples


def library_fingerprint(directory: Path, samples: list[SampleBrief], root: Path) -> str:
    """SHA-256 over every brief file and document, so two evaluations can tell whether they
    ran the same library."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yml")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    for doc in sorted({p for s in samples for p in s.documents}):
        digest.update(doc.encode() + b"\0" + (root / doc).read_bytes() + b"\0")
    return digest.hexdigest()
