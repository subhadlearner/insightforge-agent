"""Repository paths for tests, found from the root `pyproject.toml`, so no test counts directory
levels up to the repository root."""

from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("no pyproject.toml above the tests package")


REPO_ROOT = _repo_root()
BACKEND = REPO_ROOT / "src" / "backend"
PACKAGE = BACKEND / "insightforge_agent"
TESTS = BACKEND / "tests"
FIXTURES = TESTS / "fixtures"
FRONTEND = REPO_ROOT / "src" / "frontend"
