from pathlib import Path

import pytest

from insightforge_agent.stores.personas import PersonasError, load_personas

REPO_USERS = Path(__file__).resolve().parents[2] / "users.yml"


def write(tmp_path, text):
    path = tmp_path / "users.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_committed_users_file_loads_three_personas():
    users = load_personas(REPO_USERS)
    assert [u.id for u in users] == ["alice", "bob", "carol"]
    assert users[1].source_toggles == {"web": True, "documents": False, "memory": True}


def test_minimal_persona_has_no_defaults(tmp_path):
    (user,) = load_personas(write(tmp_path, "users:\n  - id: dan\n    display_name: Dan\n"))
    assert (user.id, user.display_name) == ("dan", "Dan")
    assert user.source_toggles == {} and user.webhook_url is None


def test_optional_defaults_are_loaded(tmp_path):
    text = ("users:\n  - id: dan\n    display_name: Dan\n"
            "    webhook_url: https://hooks.example.com/x\n    source_toggles: {web: false}\n")
    (user,) = load_personas(write(tmp_path, text))
    assert user.webhook_url == "https://hooks.example.com/x"
    assert user.source_toggles == {"web": False}


@pytest.mark.parametrize("field", ["api_key", "password", "token", "anthropic_api_key"])
def test_personas_carry_no_keys(tmp_path, field):
    text = f"users:\n  - id: dan\n    display_name: Dan\n    {field}: secret\n"
    with pytest.raises(PersonasError, match="no keys"):
        load_personas(write(tmp_path, text))


@pytest.mark.parametrize("text", [
    "",
    "users: []\n",
    "people:\n  - id: a\n    display_name: A\n",
    "users:\n  - display_name: NoId\n",
    "users:\n  - id: a\n",
    "users:\n  - just a string\n",
    "users:\n  - {id: a, display_name: A}\n  - {id: a, display_name: B}\n",
    "users: [unclosed\n",
])
def test_malformed_files_are_rejected(tmp_path, text):
    with pytest.raises(PersonasError):
        load_personas(write(tmp_path, text))


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(PersonasError, match="cannot read"):
        load_personas(tmp_path / "nope.yml")
