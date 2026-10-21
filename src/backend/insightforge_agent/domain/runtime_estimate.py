"""The runtime estimate shown before a Run is submitted (design.md section 10).

`base + document_count x pdf_ingest + one cost per enabled source type`. A heuristic for the
UI, not a promise: it never looks at provider latency, the Brief or what is available, so a
Run may take longer or shorter, and submission may still refuse the Brief. The coefficients
come from Settings so they can be recalibrated against real Run timings without changing the
API contract."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import get_args

from insightforge_agent.domain.plan import SourceType

SOURCE_TYPES: tuple[SourceType, ...] = get_args(SourceType)  # canonical order


@dataclass(frozen=True)
class EstimateCoefficients:
    base_seconds: int
    pdf_ingest_seconds: int  # per uploaded PDF
    source_seconds: Mapping[SourceType, int]  # per enabled source type


@dataclass(frozen=True)
class RuntimeEstimate:
    estimated_seconds: int
    base_seconds: int
    document_count: int
    source_types: list[SourceType]  # the enabled ones, in canonical order


def estimate_runtime(coefficients: EstimateCoefficients, source_toggles: Mapping[str, bool],
                     document_count: int) -> RuntimeEstimate:
    if document_count < 0:
        raise ValueError("document_count must not be negative")
    enabled = [t for t in SOURCE_TYPES if source_toggles.get(t)]
    seconds = (coefficients.base_seconds + document_count * coefficients.pdf_ingest_seconds
               + sum(coefficients.source_seconds[t] for t in enabled))
    return RuntimeEstimate(estimated_seconds=seconds, base_seconds=coefficients.base_seconds,
                           document_count=document_count, source_types=enabled)
