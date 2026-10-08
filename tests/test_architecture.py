import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINT = [str(Path(sys.executable).with_name("lint-imports"))]


def test_import_linter_contracts_hold():
    result = subprocess.run(
        LINT, cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_linter_fails_when_domain_imports_outward():
    probe = ROOT / "src/insightforge_agent/domain/_probe.py"
    probe.write_text("from insightforge_agent import stores  # noqa\n")
    try:
        result = subprocess.run(LINT, cwd=ROOT, capture_output=True, text=True)
    finally:
        probe.unlink()
    assert result.returncode != 0
