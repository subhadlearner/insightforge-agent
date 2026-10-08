"""Personas from users.yml (design.md section 9). Persona selection, not security."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from insightforge_agent.domain.models import User


class PersonasError(ValueError):
    """users.yml is missing, malformed, or contains something it must not."""


def load_personas(path: str | Path) -> list[User]:
    """Load the User personas. Each has `id`, `display_name` and optional defaults
    (`source_toggles`, `webhook_url`). Anything else, notably a key or secret, is rejected."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise PersonasError(f"cannot read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise PersonasError(f"{path} is not valid YAML: {e}") from e

    entries = data.get("users") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise PersonasError(f"{path} must contain a non-empty `users:` list")

    users = []
    for i, entry in enumerate(entries):
        try:
            users.append(_parse(entry))
        except (ValidationError, TypeError) as e:
            raise PersonasError(f"{path}: user #{i + 1} is invalid: {e}") from e

    ids = [u.id for u in users]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PersonasError(f"{path}: duplicate user ids {duplicates}")
    return users


_ALLOWED = set(User.model_fields)


def _parse(entry) -> User:
    if not isinstance(entry, dict):
        raise TypeError("each user must be a mapping")
    unknown = set(entry) - _ALLOWED
    if unknown:
        raise TypeError(f"unknown fields {sorted(unknown)} (personas carry no keys)")
    return User.model_validate(entry)
