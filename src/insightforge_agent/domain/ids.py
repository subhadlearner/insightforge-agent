"""Content-derived identifiers."""

import hashlib
from datetime import datetime


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def derive_source_id(kind: str, locator: str) -> str:
    """Derived from what identifies the Source: a URL for the web, a file's content hash for
    a PDF. It is deliberately not a hash of a web page's body: a page changes over time, and
    the Source must stay the same while it gains Observations. What a fetch contained is
    identified one level down, by `derive_observation_id`, which does hash the body."""
    return f"src_{_digest(kind, locator)}"


def derive_observation_id(owner_id: str, source: str, fetched_at: datetime, content: str) -> str:
    """Derived from the fetch itself, so storing the same fetch twice is the same Observation."""
    return f"obs_{_digest(owner_id, source, fetched_at.isoformat(), _digest(content))}"
