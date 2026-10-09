class NotFoundError(LookupError):
    """The record does not exist, or belongs to another User.

    Both cases look the same on purpose, so one User cannot probe another's data."""


class SubmissionRejected(ValueError):
    """The Brief cannot be planned at all, so no Run is created. The API answers 422."""
