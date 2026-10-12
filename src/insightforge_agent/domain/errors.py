class NotFoundError(LookupError):
    """The record does not exist, or belongs to another User.

    Both cases look the same on purpose, so one User cannot probe another's data."""


class ClarificationConflict(Exception):
    """The Run is not in the state the clarification step needs: it is not awaiting an answer
    (already answered, timed out, or never asked), or it already asked its one question. The
    API answers 409."""


class SubmissionRejected(ValueError):
    """The Brief cannot be planned at all, so no Run is created. The API answers 422."""
