"""`insightforge-agent eval`: run the sample briefs per provider and write the evaluation report.

    insightforge-agent eval --provider gemini [--provider groq] --judge-provider groq
        [--brief ID ...] [--library samples/briefs] [--out eval-results]

This makes real model and search calls with the keys in the environment and `.env`, so it is
never run by the default test suite. Each provider gets its own SQLite files under the output
directory and an in-process vector store; the app's database is never touched. One judge
scores every provider, so their synthesis scores can be compared. Writes `eval-report.json`
and `eval-report.md`. Exit 0 once the report is written, whatever it measured; 2 for a usage
or configuration error, and 1 when the judge or a provider's pipeline cannot be built: both
before any Run starts, leaving no output behind."""

import argparse
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
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
    CodeVersion,
    EvalReport,
    JudgeIdentity,
    LibraryInfo,
    ProviderResult,
    comparability_of,
    remediation_of,
    render_markdown,
    totals_of,
)
from insightforge_agent.evaluation.runner import evaluate_sample
from insightforge_agent.llm import (
    CHAT_PROVIDERS,
    ROLES,
    build_chat_model,
    configured_model_name,
)
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.wiring import build_deps
from insightforge_agent.stores.sqlite import SqliteRepos

JUDGE_ROLE = "writer"  # the judge is the judge provider's Writer model
# Generation settings a chat model may carry; what it does not set is the provider's default.
INFERENCE_FIELDS = ("temperature", "max_tokens", "max_output_tokens", "top_p", "top_k")

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
    p.add_argument("--provider", action="append", choices=CHAT_PROVIDERS, required=True,
                   help="a provider to evaluate; repeat to evaluate several")
    p.add_argument("--judge-provider", choices=(*CHAT_PROVIDERS, "none"), required=True,
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


def _code_version(root: Path) -> CodeVersion:
    status = _git(root, "status", "--porcelain")
    return CodeVersion(git_commit=_git(root, "rev-parse", "HEAD"),
                       git_dirty=None if status is None else bool(status))


def _model_id(component: object) -> str:
    """The class that ran, with the model id it carries, if any (a scripted model has none)."""
    name = getattr(component, "model", None) or getattr(component, "model_name", None)
    name = name if isinstance(name, str) else None
    return f"{type(component).__name__}:{name}" if name else type(component).__name__


def _inference(model: BaseChatModel) -> dict:
    values = {f: getattr(model, f, None) for f in INFERENCE_FIELDS}
    return {f: v for f, v in values.items() if isinstance(v, (int, float, str, bool))}


def _conditions(settings: Settings, deps: Deps) -> dict:
    """The pipeline settings the Runs ran under, read from the Deps they used."""
    return {
        # As configured; what the Runs actually used is in `runtime_services`.
        "configured_search_providers": ",".join(settings.usable_search_providers),
        "configured_embedding_model": settings.embedding_model,
        "fact_check_rate": deps.fact_check_rate,
        "fact_check_minimum": deps.fact_check_minimum,
        "similarity_threshold": deps.similarity_threshold,
        "evidence_budget_tokens": deps.evidence_budget_tokens,
        "passage_token_cap": deps.passage_token_cap,
        "run_token_cap": deps.run_token_cap,
        "writer_retries": deps.writer_retries,
        # The effective Evidence thresholds: they set Confidence and ranking.
        "credibility_high": deps.thresholds.credibility_high,
        "credibility_medium": deps.thresholds.credibility_medium,
        "recency_fresh_days": deps.thresholds.recency_fresh_days,
        "recency_recent_days": deps.thresholds.recency_recent_days,
        "clarification": "off" if deps.timeouts is None else "on",
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
    info = LibraryInfo(directory=str(opts.library), briefs_in_library=len(library),
                       sample_ids=[s.id for s in samples],
                       fingerprint=library_fingerprint(opts.library, library, root))

    started = now()
    out = opts.out / started.strftime("%Y%m%dT%H%M%SZ")
    judge, identity = None, JudgeIdentity(provider="none", rubric_version=RUBRIC_VERSION)
    if judge_settings is not None:
        try:
            judge = make_judge(judge_settings)
        except Exception as e:  # noqa: BLE001 - reported; nothing has been written or run
            return _cannot_build(f"the {opts.judge_provider} judge", e)
        identity = JudgeIdentity(
            provider=opts.judge_provider, rubric_version=RUBRIC_VERSION,
            model=configured_model_name(JUDGE_ROLE, judge_settings),
            runtime_model=_model_id(judge), role=JUDGE_ROLE, inference=_inference(judge))
    with ExitStack() as stack:
        # Every provider is wired before any Run starts, so a construction failure costs no
        # provider calls and leaves no partial evaluation behind.
        out.mkdir(parents=True, exist_ok=False)
        built = {}
        for provider in opts.provider:
            try:
                built[provider] = _open(settings[provider], out / provider, make_deps, stack)
            except Exception as e:  # noqa: BLE001
                stack.close()
                shutil.rmtree(out)
                return _cannot_build(f"the {provider} pipeline", e)
        results = []
        for provider, deps in built.items():
            print(f"eval: {provider}: running {len(samples)} brief(s)", file=sys.stderr)
            results.append(_evaluate(provider, settings[provider], deps, samples, info, root,
                                     out / provider, judge))
    report = EvalReport(generated_at=started, library=info, code=_code_version(root),
                        judge=identity, providers=results,
                        comparability=comparability_of(results))
    (out / "eval-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / "eval-report.md").write_text(render_markdown(report), encoding="utf-8")
    print(out / "eval-report.md")
    return 0


def _open(settings: Settings, workdir: Path, make_deps: MakeDeps, stack: ExitStack) -> Deps:
    """One provider's own SQLite files and vector store, and its Deps over them; `stack`
    closes them."""
    workdir.mkdir(parents=True)
    repos = SqliteRepos(str(workdir / "eval.db"))
    stack.callback(repos.close)
    ck = sqlite3.connect(str(workdir / "checkpoints.db"), check_same_thread=False)
    stack.callback(ck.close)
    qdrant = QdrantClient(":memory:")
    stack.callback(qdrant.close)
    return make_deps(settings, repos, SqliteSaver(ck), qdrant)


def _evaluate(provider: str, settings: Settings, deps: Deps, samples, library: LibraryInfo,
              root: Path, workdir: Path, judge: BaseChatModel | None) -> ProviderResult:
    sample_results = []
    for sample in samples:
        print(f"eval: {provider}: {sample.id}", file=sys.stderr)
        sample_results.append(evaluate_sample(deps, sample, root, judge))
    totals = totals_of(sample_results)
    return ProviderResult(
        provider=provider,
        configured_models={role: configured_model_name(role, settings) for role in ROLES},
        runtime_models={role: _model_id(getattr(deps, f"{role}_model"))
                        for role in ("planner", "writer", "light", "researcher")},
        runtime_services={"embedder": _model_id(deps.embedder),
                          "search": _model_id(deps.search), "fetcher": _model_id(deps.fetcher)},
        conditions=_conditions(settings, deps), workdir=str(workdir), samples=sample_results,
        totals=totals, remediation=remediation_of(provider, sample_results, totals, library))


def _usage(message: str) -> int:
    print(f"insightforge-agent eval: error: {message}", file=sys.stderr)
    return 2


def _cannot_build(what: str, error: Exception) -> int:
    print(f"insightforge-agent eval: error: could not build {what}: {type(error).__name__}: "
          f"{error}; no Run was started and no report was written", file=sys.stderr)
    return 1
