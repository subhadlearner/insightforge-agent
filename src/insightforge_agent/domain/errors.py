class NotFoundError(LookupError):
    """The record does not exist, or belongs to another User.

    Both cases look the same on purpose, so one User cannot probe another's data."""


class ClarificationConflict(Exception):
    """The Run is not in the state the clarification step needs: it is not awaiting an answer
    (already answered, timed out, or never asked), or it already asked its one question. The
    answer endpoint answers 409."""


class SubmissionRejected(ValueError):
    """The Brief cannot be planned at all, so no Run is created. The API answers 422."""


class ProvenanceError(Exception):
    """A stored Report's provenance is inconsistent: a Claim names an Evidence or Historical
    item the Report does not hold, a cited Passage, Observation or Source is missing, or the
    body is malformed. Citations are never fabricated or skipped for it; the citations
    endpoint answers 500. Deliberately not a NotFoundError: the Report itself exists."""
