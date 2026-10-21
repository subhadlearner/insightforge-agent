"""`insightforge-agent eval-judge` (T18 CS5): judge the Reports a finished evaluation stored,
read-only, into a separate judge-only report. The source evaluation is never written to and its
results are never replaced; scripted models only, no provider is called."""

import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.evaluation import judge_only
from insightforge_agent.evaluation.judge import CRITERIA, JUDGE_PROMPT_VERSION, RUBRIC_VERSION
from tests.evaluation.world import World
from tests.scripted import ScriptedChat

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "samples" / "briefs"
SHEET = json.dumps({**{c: 3 for c in CRITERIA}, "rationale": "Covers the brief."})


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A complete scripted evaluation run with no judge, as T18's directory layout."""
    from insightforge_agent.evaluation.cli import main

    tmp = tmp_path_factory.mktemp("src")
    mp = pytest.MonkeyPatch()
    for key in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY"):
        mp.setenv(key, "test-key-not-used")
    mp.setenv("SEARCH_PROVIDERS", "tavily")
    mp.chdir(tmp)
    try:
        world = World()
        code = main(["--library", str(LIBRARY), "--out", str(tmp / "out"), "--provider",
                     "anthropic", "--judge-provider", "none"], make_deps=world.make_deps,
                    root=ROOT)
    finally:
        mp.undo()
    assert code == 0
    [run_dir] = list((tmp / "out").iterdir())
    return run_dir


def digest(directory: Path) -> dict[str, str]:
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


class Judges:
    def __init__(self, reply=SHEET):
        self.reply, self.models = reply, []

    def __call__(self, settings):
        self.models.append(ScriptedChat(reply=lambda messages: AIMessage(content=self.reply)))
        return self.models[-1]


def run(tmp_path, source, judges, *extra):
    out = tmp_path / "judged"
    code = judge_only.main(["--from", str(source), "--judge-provider", "anthropic",
                            "--library", str(LIBRARY), "--out", str(out), *extra],
                           make_judge=judges, root=ROOT)
    return code, out


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.setenv(key, "test-key-not-used")
    monkeypatch.setenv("SEARCH_PROVIDERS", "tavily")


def test_stored_reports_are_judged_into_a_separate_report_and_the_source_is_untouched(
        tmp_path, source):
    before = digest(source)
    judges = Judges()
    code, out = run(tmp_path, source, judges)
    assert code == 0 and digest(source) == before  # read-only: byte-identical
    [judged_dir] = list(out.iterdir())
    report = json.loads((judged_dir / "judge-report.json").read_text(encoding="utf-8"))
    original = json.loads((source / "eval-report.json").read_text(encoding="utf-8"))
    assert report["source"]["eval_report_sha256"] == hashlib.sha256(
        (source / "eval-report.json").read_bytes()).hexdigest()
    assert report["source"]["judge"]["provider"] == "none"  # what the source recorded
    assert (report["judge"]["rubric_version"], report["judge"]["prompt_version"]) == (
        RUBRIC_VERSION, JUDGE_PROMPT_VERSION)
    complete = [(p["provider"], s["sample_id"], s["report_id"]) for p in original["providers"]
                for s in p["samples"] if s["status"] == "complete"]
    assert [(r["provider"], r["sample_id"], r["report_id"]) for r in report["results"]] == complete
    assert {r["synthesis"]["status"] for r in report["results"]} == {"judged"}
    assert len(judges.models[0].seen) == len(complete)  # one call per stored Report


def test_a_refused_reply_is_recorded_with_its_error_type_and_never_scored(tmp_path, source):
    long = json.dumps({**{c: 3 for c in CRITERIA}, "rationale": "x" * 601})
    code, out = run(tmp_path, source, Judges(long))
    [judged_dir] = list(out.iterdir())
    results = json.loads((judged_dir / "judge-report.json").read_text())["results"]
    assert code == 0 and {r["synthesis"]["status"] for r in results} == {"error"}
    assert all("string_too_long" in r["synthesis"]["reason"] and r["synthesis"]["score"] is None
               for r in results)


def test_a_library_that_differs_from_the_evaluated_one_is_refused(tmp_path, source):
    library = tmp_path / "lib"
    import shutil
    shutil.copytree(LIBRARY, library)
    first = next(library.glob("*.yml"))
    first.write_text(first.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    out = tmp_path / "judged"
    code = judge_only.main(["--from", str(source), "--judge-provider", "anthropic", "--library",
                            str(library), "--out", str(out)], make_judge=Judges(), root=ROOT)
    assert code == 2 and not out.exists()


def test_a_directory_without_a_published_evaluation_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = run(tmp_path, empty, Judges())
    assert code == 2 and not out.exists()


def test_a_missing_provider_database_is_an_error_result_not_a_crash(tmp_path, source):
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(source, copy)
    for db in copy.rglob("eval.db"):
        db.unlink()
    code, out = run(tmp_path, copy, Judges())
    [judged_dir] = list(out.iterdir())
    results = json.loads((judged_dir / "judge-report.json").read_text())["results"]
    assert code == 0 and results
    assert {r["synthesis"]["status"] for r in results} == {"error"}
    assert all("could not be found" in r["synthesis"]["reason"] for r in results)
