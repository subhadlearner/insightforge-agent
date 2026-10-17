"""`insightforge-agent eval`: run the sample briefs per provider and write the evaluation report.

    insightforge-agent eval --provider gemini [--provider groq] --judge-provider groq
        [--brief ID ...] [--library samples/briefs] [--out eval-results]

This makes real model and search calls with the keys in the environment and `.env`, so it is
never run by the default test suite. Each provider gets its own SQLite files under the output
directory and an in-process vector store; the app's database is never touched. One judge
scores every provider, so their synthesis scores can be compared. Writes `eval-report.json`
and `eval-report.md`. Exit 0 once the report is written, whatever it measured; 2 for a usage
or configuration error, before any Run starts."""

import argparse
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite import SqliteSaver
from qdrant_client import QdrantClient

from insightforge_agent.config import Settings
from insightforge_agent.evaluation.judge import RUBRIC_VERSION
from insightforge_agent.evaluation.library import (
    LibraryError,
    library_fingerprint,
    load_library,
)
from insightforge_agent.evaluation.report import (
    EvalReport,
    ProviderResult,
    comparability_of,
    remediation_of,
    render_markdown,
    totals_of,
)
from insightforge_agent.evaluation.runner import evaluate_sample
from insightforge_agent.llm import build_chat_model
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.wiring import build_deps
from insightforge_agent.stores.sqlite import SqliteRepos

PROVIDERS = ("anthropic", "gemini", "groq")
ROLES = ("planner", "writer", "light")  # the Settings model names build_chat_model reads
JUDGE_ROLE = "writer"  # the judge is the judge provider's Writer model, with its defaults

# Builds one provider's Deps over that provider's repositories, checkpointer and vector store.
MakeDeps = Callable[[Settings, SqliteRepos, SqliteSaver, QdrantClient], Deps]
MakeJudge = Callable[[Settings], BaseChatModel]


def _production_deps(settings: Settings, repos: SqliteRepos, checkpointer: SqliteSaver,
                     qdrant: QdrantClient) -> Deps:
    # No timeout scheduler: a Planner question is refused and the Brief is planned as given.
    return build_deps(settings, repos, checkpointer, timeouts=None, qdrant=qdrant)


def _production_judge(settings: Settings) -> BaseChatModel:
    return build_chat_model(JUDGE_ROLE, settings)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="insightforge-agent eval",
        description="Run the sample briefs per provider and write the evaluation report. "
                    "Makes real, possibly paid, model and search calls.")
    p.add_argument("--provider", action="append", choices=PROVIDERS, required=True,
                   help="a provider to evaluate; repeat to evaluate several")
    p.add_argument("--judge-provider", choices=(*PROVIDERS, "none"), required=True,
                   help="the one provider whose Writer model judges synthesis for every "
                        "evaluated provider; none records synthesis as not judged")
    p.add_argument("--brief", action="append", default=None,
                   help="run only this sample id (repeatable); the report says it is partial")
    p.add_argument("--library", type=Path, default=Path("samples/briefs"))
    p.add_argument("--out", type=Path, default=Path("eval-results"))
    return p


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _model_id(component: object) -> str:
    """The class that ran, with the model id it carries, if any (a scripted model has none)."""
    name = getattr(component, "model", None) or getattr(component, "model_name", None)
    name = name if isinstance(name, str) else None
    return f"{type(component).__name__}:{name}" if name else type(component).__name__


def _conditions(settings: Settings, root: Path) -> dict:
    status = _git(root, "status", "--porcelain")
    return {
        "git_commit": _git(root, "rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
        # As configured; what each provider's Runs actually used is in its `runtime_services`.
        "configured_search_providers": ",".join(settings.usable_search_providers),
        "configured_embedding_model": settings.embedding_model,
        "fact_check_rate": settings.fact_check_rate,
        "fact_check_minimum": settings.fact_check_minimum,
        "similarity_threshold": settings.similarity_threshold,
        "evidence_budget_tokens": settings.evidence_budget_tokens,
        "passage_token_cap": settings.passage_token_cap,
        "run_token_cap": settings.run_token_cap,
        "clarification": "off",
        "vector_store": "in-process",
    }


def main(argv: list[str], *, make_deps: MakeDeps | None = None,
         make_judge: MakeJudge | None = None, root: Path | None = None,
         now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    """`make_deps` and `make_judge` replace the real models and search (tests pass scripted
    ones); everything else is the production path."""
    opts = _parser().parse_args(argv)
    root = root or Path.cwd()
    make_deps = make_deps or _production_deps
    make_judge = make_judge or _production_judge
    if len(set(opts.provider)) != len(opts.provider):
        return _usage("each --provider may be given once")
    try:
        library = load_library(opts.library, root, size=None)
        settings = {p: Settings(llm_provider=p) for p in opts.provider}
        judge_settings = (None if opts.judge_provider == "none"
                          else Settings(llm_provider=opts.judge_provider))
    except (LibraryError, ValueError) as e:
        return _usage(str(e))
    samples = library
    if opts.brief:
        known = {s.id for s in library}
        unknown = [b for b in opts.brief if b not in known]
        if unknown:
            return _usage(f"unknown sample id(s): {', '.join(unknown)}")
        samples = [s for s in library if s.id in set(opts.brief)]

    started = now()
    out = opts.out / started.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=False)
    judge = make_judge(judge_settings) if judge_settings is not None else None
    first = settings[opts.provider[0]]
    results = []
    for provider in opts.provider:
        print(f"eval: {provider}: running {len(samples)} brief(s)", file=sys.stderr)
        results.append(_evaluate(provider, settings[provider], samples, root, out / provider,
                                 make_deps, judge))
    report = EvalReport(
        generated_at=started,
        library={"directory": str(opts.library), "briefs_in_library": len(library),
                 "sample_ids": [s.id for s in samples],
                 "fingerprint": library_fingerprint(opts.library, library, root)},
        conditions=_conditions(first, root),
        judge={"provider": opts.judge_provider, "rubric_version": RUBRIC_VERSION,
               "model": (getattr(judge_settings, f"{opts.judge_provider}_{JUDGE_ROLE}_model")
                         if judge_settings else None),
               "runtime_model": _model_id(judge) if judge is not None else None,
               "role": JUDGE_ROLE if judge_settings else None,
               "sampling": "provider defaults" if judge_settings else None},
        providers=results,
        comparability=comparability_of(results))
    (out / "eval-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / "eval-report.md").write_text(render_markdown(report), encoding="utf-8")
    print(out / "eval-report.md")
    return 0


def _evaluate(provider: str, settings: Settings, samples, root: Path, workdir: Path,
              make_deps: MakeDeps, judge: BaseChatModel | None) -> ProviderResult:
    workdir.mkdir(parents=True)
    repos = SqliteRepos(str(workdir / "eval.db"))
    ck = sqlite3.connect(str(workdir / "checkpoints.db"), check_same_thread=False)
    qdrant = QdrantClient(":memory:")
    try:
        deps = make_deps(settings, repos, SqliteSaver(ck), qdrant)
        sample_results = []
        for sample in samples:
            print(f"eval: {provider}: {sample.id}", file=sys.stderr)
            sample_results.append(evaluate_sample(deps, sample, root, judge))
        runtime = {role: _model_id(getattr(deps, f"{role}_model"))
                   for role in ("planner", "writer", "light", "researcher")}
        services = {"embedder": _model_id(deps.embedder), "search": _model_id(deps.search),
                    "fetcher": _model_id(deps.fetcher)}
    finally:
        qdrant.close()
        ck.close()
        repos.close()
    totals = totals_of(sample_results)
    return ProviderResult(
        provider=provider,
        configured_models={role: getattr(settings, f"{provider}_{role}_model") for role in ROLES},
        runtime_models=runtime, runtime_services=services, workdir=str(workdir), samples=sample_results, totals=totals,
        remediation=remediation_of(provider, sample_results, totals))


def _usage(message: str) -> int:
    print(f"insightforge-agent eval: error: {message}", file=sys.stderr)
    return 2
