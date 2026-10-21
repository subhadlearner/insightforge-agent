"""The repository layout the tests and the PDF renderer rely on: `src/` holds exactly `backend/`
and `frontend/`, and shared data stays at the repository root."""

from insightforge_agent.pipeline.report_delivery import (
    SubprocessPlaywrightRenderer,
    default_render_script,
)
from tests.paths import BACKEND, FIXTURES, FRONTEND, PACKAGE, REPO_ROOT, TESTS


def test_src_holds_only_backend_and_frontend():
    assert sorted(p.name for p in (REPO_ROOT / "src").iterdir() if p.is_dir()) == [
        "backend", "frontend"]


def test_paths_point_at_the_moved_trees():
    assert (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "users.yml").is_file()
    assert (PACKAGE / "__init__.py").is_file() and PACKAGE.parent == BACKEND
    assert (TESTS / "conftest.py").is_file() and FIXTURES.is_dir()
    assert (FRONTEND / "package.json").is_file()


def test_shared_data_stays_at_the_repository_root():
    assert len(list((REPO_ROOT / "samples" / "briefs").glob("*.yml"))) == 5
    assert (REPO_ROOT / "benchmarks" / "evidence" / "cases.jsonl").is_file()


def test_the_default_pdf_renderer_is_the_frontend_script():
    script = FRONTEND / "scripts" / "render-pdf.mjs"
    assert default_render_script() == script and script.is_file()
    assert SubprocessPlaywrightRenderer().script == script
