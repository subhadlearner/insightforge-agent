"""The benchmark -> production boundary. Baseline adapters (M1.3) may call the real T5/T6 logic,
but nothing in the benchmark may build a model, open a network connection or run the graph, and
production code never imports the benchmark. Each test writes a probe module, runs
`lint-imports`, and removes the probe."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "insightforge_agent"
LINT = [str(Path(sys.executable).with_name("lint-imports"))]


def lint(relative: str, source: str) -> subprocess.CompletedProcess:
    probe = SRC / relative
    probe.write_text(source + "\n")
    try:
        return subprocess.run(LINT, cwd=ROOT, capture_output=True, text=True)
    finally:
        probe.unlink()


@pytest.mark.parametrize("source", [
    "from insightforge_agent.pipeline import synthesize  # noqa",
    "from insightforge_agent.pipeline import fact_check  # noqa",
    "from insightforge_agent.pipeline import extract, write, deps, citations  # noqa",
    "from insightforge_agent.domain import evidence, quantities, passages, periods  # noqa",
    "from insightforge_agent.embeddings import HashingEmbedder  # noqa",
    "from insightforge_agent.stores.memory import MemoryRepos  # noqa",
    "from insightforge_agent.stores.source_store import SourceStore  # noqa",
])
def test_the_benchmark_may_use_the_real_t5_t6_logic(source):
    result = lint("benchmark/_probe.py", source)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("source,contract", [
    ("from insightforge_agent import llm  # noqa", "model construction"),
    ("from insightforge_agent.pipeline import wiring  # noqa", "model construction"),
    ("from insightforge_agent.pipeline import graph  # noqa", "model construction"),
    ("from insightforge_agent.agents import web  # noqa", "no live, network or service"),
    ("from insightforge_agent.agents import planner  # noqa", "no live, network or service"),
    ("from insightforge_agent.stores import sqlite  # noqa", "no live, network or service"),
    ("from insightforge_agent.pipeline import dispatch  # noqa", "no live, network or service"),
    ("from insightforge_agent import config  # noqa", "no live, network or service"),
    ("from insightforge_agent import api  # noqa", "no live, network or service"),
    ("from insightforge_agent import demo  # noqa", "no live, network or service"),
])
def test_the_benchmark_cannot_build_models_reach_the_network_or_run_the_graph(source, contract):
    result = lint("benchmark/_probe.py", source)
    assert result.returncode != 0 and contract in result.stdout.replace("\n", " ")


def test_the_model_and_settings_modules_stay_unreachable_even_through_allowed_modules():
    # An allowed production module that began importing llm would drag the benchmark onto the
    # paid path; the chain check catches it.
    allowed = SRC / "pipeline" / "citations.py"
    original = allowed.read_text(encoding="utf-8")
    allowed.write_text(original + "\nfrom insightforge_agent import llm  # noqa\n", encoding="utf-8")
    try:
        benchmark_probe = lint_with_existing_probe()
    finally:
        allowed.write_text(original, encoding="utf-8")
    assert benchmark_probe.returncode != 0
    assert "benchmark" in benchmark_probe.stdout and "llm" in benchmark_probe.stdout


def lint_with_existing_probe() -> subprocess.CompletedProcess:
    return lint("benchmark/_probe.py", "from insightforge_agent.pipeline import citations  # noqa")


@pytest.mark.parametrize("package", ["pipeline", "domain", "agents", "stores"])
def test_production_code_still_cannot_import_the_benchmark(package):
    result = lint(f"{package}/_probe.py", "from insightforge_agent.benchmark import cases  # noqa")
    assert result.returncode != 0 and "never imports the benchmark" in result.stdout


def test_allowing_synthesis_does_not_let_production_import_the_benchmark_indirectly():
    result = lint("pipeline/_probe.py", "from insightforge_agent.benchmark import stats  # noqa")
    assert result.returncode != 0


def test_no_benchmark_module_imports_a_model_client_library():
    forbidden = ("anthropic", "langchain", "httpx", "requests", "openai", "torch", "laya")
    for path in (SRC / "benchmark").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("import ", "from ")):
                module = line.split()[1].split(".")[0]
                assert module not in forbidden, f"{path.name}: {line}"
