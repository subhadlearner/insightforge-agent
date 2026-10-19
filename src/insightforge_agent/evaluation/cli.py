"""`insightforge-agent eval`: run the sample briefs per provider and write the evaluation report.

    insightforge-agent eval --provider gemini [--provider groq] --judge-provider groq
        [--brief ID ...] [--library samples/briefs] [--out eval-results]

This makes real model and search calls with the keys in the environment and `.env`, so it is
never run by the default test suite. Each provider gets its own SQLite files under the output
directory and an in-process vector store; the app's database is never touched. One judge
scores every provider, so their synthesis scores can be compared.

Each evaluation gets a new directory, `<out>/<UTC timestamp>`, or `<timestamp>-1`, `-2` ...
when an evaluation started in the same second already has one; an existing one is never
reused. Both reports are prepared, then written to temporary files in that directory and
published by rename: `eval-report.md` first, `eval-report.json` last, so `eval-report.json`
exists only for a complete evaluation.

Exit 0 once both reports are published, whatever they measured; 2 for a usage or configuration
error; 1 when the judge, a provider's pipeline or the evaluation directory cannot be set up,
before any Run starts and leaving no output behind; 3 when the Runs finished but the reports
could not be prepared or published: no `eval-report.json` is left, and the Runs' SQLite files
are kept in the evaluation directory. An unexpected error while the Runs execute (or Ctrl+C)
ends the command with Python's traceback and exit 1; it, too, publishes no `eval-report.json`
and keeps the stores."""

import argparse
import os
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
from insightforge_agent.evaluation.judge import JUDGE_PROMPT_VERSION, RUBRIC_VERSION
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
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MAX_DIR_ATTEMPTS = 100  # <stamp>, then <stamp>-1 ... <stamp>-99

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
        # T18 remediation behaviours: a run under them is never the T18 baseline.
        "draft_structured_output": "on" if deps.native_json is not None else "off",
        "draft_passage_cap": deps.draft_passage_cap,
        "fact_check_method": "f2+counterpart-v1" if deps.fact_check_counterpart else "f2",
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
    judge, identity = None, JudgeIdentity(provider="none", rubric_version=RUBRIC_VERSION)
    if judge_settings is not None:
        try:
            judge = make_judge(judge_settings)
        except Exception as e:  # noqa: BLE001 - reported; nothing has been written or run
            return _cannot_build(f"the {opts.judge_provider} judge", e)
        identity = JudgeIdentity(
            provider=opts.judge_provider, rubric_version=RUBRIC_VERSION,
            prompt_version=JUDGE_PROMPT_VERSION, model=configured_model_name(JUDGE_ROLE, judge_settings),
            runtime_model=_model_id(judge), role=JUDGE_ROLE, inference=_inference(judge))
    try:
        out = allocate_evaluation_dir(opts.out, started.strftime(STAMP_FORMAT))
    except OSError as e:
        return _no_directory(opts.out, e)
    with ExitStack() as stack:
        # Every provider is wired before any Run starts, so a construction failure costs no
        # provider calls and leaves no partial evaluation behind.
        built = {}
        for provider in opts.provider:
            try:
                built[provider] = _open(settings[provider], out / provider, make_deps, stack)
            except Exception as e:  # noqa: BLE001
                # The cause is reported first; a clean-up failure after it, never instead.
                code = _cannot_build(f"the {provider} pipeline", e)
                try:
                    stack.close()  # runs every close, even after one raises
                except Exception as closing:  # noqa: BLE001 - any close may fail; reported
                    print(f"insightforge-agent eval: error: could not close the stores in "
                          f"{out}: {type(closing).__name__}: {closing}", file=sys.stderr)
                try:
                    shutil.rmtree(out)
                except OSError as cleanup:
                    print(f"insightforge-agent eval: error: could not remove {out}: "
                          f"{type(cleanup).__name__}: {cleanup}", file=sys.stderr)
                return code
        results = []
        for provider, deps in built.items():
            print(f"eval: {provider}: running {len(samples)} brief(s)", file=sys.stderr)
            results.append(_evaluate(provider, settings[provider], deps, samples, info, root,
                                     out / provider, judge))
    return _publish(out, lambda: EvalReport(
        generated_at=started, library=info, code=_code_version(root), judge=identity,
        providers=results, comparability=comparability_of(results)))


def _publish(out: Path, build: Callable[[], EvalReport]) -> int:
    """Both reports, prepared in full, then published by rename: Markdown first, JSON last.
    On failure, whatever of them was written is removed where possible; the Runs' stores in
    `out` are always kept."""
    json_path, md_path = out / "eval-report.json", out / "eval-report.md"
    try:
        report = build()
        texts = {md_path: render_markdown(report), json_path: report.model_dump_json(indent=2)}
        for text in texts.values():
            text.encode("utf-8")  # an unencodable character fails here, before any write
    except Exception as e:  # noqa: BLE001 - reported (exit 3); the Runs' stores are kept
        return _not_published(out, "could not be prepared", e, [])
    temps = {final: out / f".{final.name}.tmp" for final in texts}
    published: list[Path] = []
    try:
        for final, text in texts.items():
            _write_synced(temps[final], text)
        for final in (md_path, json_path):  # eval-report.json last: it marks completion
            os.replace(temps[final], final)
            published.append(final)
    except OSError as e:
        return _not_published(out, "could not be written", e, [*temps.values(), *published])
    print(md_path)
    return 0


def _write_synced(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _not_published(out: Path, what: str, error: Exception, to_remove: list[Path]) -> int:
    """Remove what may have been written of the reports, then report the failure; a file
    that cannot be removed is listed after the cause, never instead of it."""
    stuck = []
    for path in to_remove:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            stuck.append(f"{path} ({type(e).__name__}: {e})")
    print(f"insightforge-agent eval: error: the Runs finished but the reports {what}: "
          f"{type(error).__name__}: {error}; no eval-report.json was published. The Runs' "
          f"SQLite files are kept in {out}"
          + (f"; could not remove: {', '.join(stuck)}" if stuck else ""), file=sys.stderr)
    return 3


def allocate_evaluation_dir(base: Path, stamp: str) -> Path:
    """Claim a new, empty directory for one evaluation: `<base>/<stamp>`, or `<stamp>-1`,
    `<stamp>-2` ... when another evaluation (in this or another process) already has it.
    Each name is claimed by one `mkdir`, which fails if the name exists, so two evaluations
    never share a directory and an existing one is never reused. FileExistsError once
    MAX_DIR_ATTEMPTS names are taken; any other OSError is raised as it is."""
    base.mkdir(parents=True, exist_ok=True)
    for n in range(MAX_DIR_ATTEMPTS):
        candidate = base / (stamp if n == 0 else f"{stamp}-{n}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"{base / stamp} and its {MAX_DIR_ATTEMPTS - 1} numbered "
                          f"alternatives already exist; choose another --out")


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


def _no_directory(base: Path, error: OSError) -> int:
    print(f"insightforge-agent eval: error: could not create the evaluation directory under "
          f"{base}: {type(error).__name__}: {error}; no Run was started and no report was "
          f"written", file=sys.stderr)
    return 1
