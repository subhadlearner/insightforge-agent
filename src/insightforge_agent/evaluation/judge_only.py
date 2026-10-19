"""`insightforge-agent eval-judge`: judge the Reports a finished evaluation stored (T18 CS5).

    insightforge-agent eval-judge --from <evaluation dir> --judge-provider anthropic
        [--library samples/briefs] [--out eval-results]

Reads `<from>/eval-report.json` and each provider's `<from>/<provider>/eval.db` read-only, calls
the judge once per stored Report of a complete sample, and publishes a separate
`judge-report.json` in a new directory under `--out`. The source evaluation is never written to,
and its recorded synthesis results are never replaced: the judge-only result is a new
measurement, identified by the source report's SHA-256 and by the judge's rubric and prompt
versions. This makes real, possibly paid, judge calls.

Exit 0 once the report is published; 2 for a usage error (no published evaluation in `--from`,
or a library that differs from the one evaluated); 1 when the judge or the output directory
cannot be set up; 3 when the report cannot be written."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from insightforge_agent.config import Settings
from insightforge_agent.domain.models import Report
from insightforge_agent.evaluation.cli import (
    JUDGE_ROLE,
    STAMP_FORMAT,
    _code_version,
    _inference,
    _model_id,
    _production_judge,
    allocate_evaluation_dir,
)
from insightforge_agent.evaluation.judge import (
    JUDGE_PROMPT_VERSION,
    RUBRIC_VERSION,
    SynthesisResult,
    judge_synthesis,
)
from insightforge_agent.evaluation.library import LibraryError, library_fingerprint, load_library
from insightforge_agent.evaluation.report import CodeVersion, EvalReport, JudgeIdentity
from insightforge_agent.llm import CHAT_PROVIDERS, configured_model_name

MakeJudge = Callable[[Settings], BaseChatModel]


class SourceEvaluation(BaseModel):
    directory: str
    eval_report_sha256: str
    generated_at: datetime
    code: CodeVersion
    judge: JudgeIdentity  # the judge the source evaluation recorded


class JudgedReport(BaseModel):
    provider: str
    sample_id: str
    report_id: str
    synthesis: SynthesisResult


class JudgeOnlyReport(BaseModel):
    kind: str = "judge-only"
    generated_at: datetime
    source: SourceEvaluation
    code: CodeVersion
    judge: JudgeIdentity
    results: list[JudgedReport]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="insightforge-agent eval-judge",
        description="Judge the Reports a finished evaluation stored, into a separate "
                    "judge-only report. Makes real, possibly paid, judge calls.")
    p.add_argument("--from", dest="source", type=Path, required=True,
                   help="a published evaluation directory (it holds eval-report.json)")
    p.add_argument("--judge-provider", choices=CHAT_PROVIDERS, required=True)
    p.add_argument("--library", type=Path, default=Path("samples/briefs"))
    p.add_argument("--out", type=Path, default=Path("eval-results"))
    return p


def _error(message: str, code: int) -> int:
    print(f"insightforge-agent eval-judge: error: {message}", file=sys.stderr)
    return code


def _stored_report(db: Path, owner_id: str, report_id: str) -> Report | None:
    """The stored Report, read through a read-only connection; None if it is not there."""
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT id, owner_id, run_id, created_at, body FROM reports "
                          "WHERE owner_id=? AND id=?", (owner_id, report_id)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return Report(id=row[0], owner_id=row[1], run_id=row[2], created_at=row[3],
                  body=json.loads(row[4]))


def main(argv: list[str], *, make_judge: MakeJudge | None = None, root: Path | None = None,
         now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    opts = _parser().parse_args(argv)
    root = root or Path.cwd()
    make_judge = make_judge or _production_judge
    source_json = opts.source / "eval-report.json"
    try:
        raw = source_json.read_bytes()
        source = EvalReport.model_validate_json(raw)
    except (OSError, ValidationError) as e:
        return _error(f"{source_json} is not a published evaluation report: "
                      f"{type(e).__name__}", 2)
    try:
        library = load_library(opts.library, root, size=None)
        settings = Settings(llm_provider=opts.judge_provider)
    except (LibraryError, ValueError) as e:
        return _error(str(e), 2)
    if library_fingerprint(opts.library, library, root) != source.library.fingerprint:
        return _error("the library differs from the one the source evaluation ran "
                      "(fingerprint mismatch); the expected structures would not match", 2)
    samples = {s.id: s for s in library}
    try:
        judge = make_judge(settings)
    except Exception as e:  # noqa: BLE001 - reported; nothing has been written or called
        return _error(f"could not build the {opts.judge_provider} judge: "
                      f"{type(e).__name__}: {e}", 1)
    identity = JudgeIdentity(
        provider=opts.judge_provider, rubric_version=RUBRIC_VERSION,
        prompt_version=JUDGE_PROMPT_VERSION,
        model=configured_model_name(JUDGE_ROLE, settings), runtime_model=_model_id(judge),
        role=JUDGE_ROLE, inference=_inference(judge))
    started = now()
    results = []
    for provider in source.providers:
        db = opts.source / provider.provider / "eval.db"
        for s in provider.samples:
            if s.status != "complete" or not s.report_id:
                continue
            report = _stored_report(db, s.owner_id, s.report_id)
            synthesis = (judge_synthesis(judge, samples[s.sample_id], report)
                         if report is not None and s.sample_id in samples else
                         SynthesisResult(status="error", reason="the stored Report or its "
                                         "sample brief could not be found"))
            results.append(JudgedReport(provider=provider.provider, sample_id=s.sample_id,
                                        report_id=s.report_id, synthesis=synthesis))
    out_report = JudgeOnlyReport(
        generated_at=started, code=_code_version(root), judge=identity, results=results,
        source=SourceEvaluation(directory=str(opts.source),
                                eval_report_sha256=hashlib.sha256(raw).hexdigest(),
                                generated_at=source.generated_at, code=source.code,
                                judge=source.judge))
    try:
        out = allocate_evaluation_dir(opts.out, started.strftime(STAMP_FORMAT))
    except OSError as e:
        return _error(f"could not create the output directory under {opts.out}: {e}", 1)
    final, temp = out / "judge-report.json", out / ".judge-report.json.tmp"
    try:
        with temp.open("w", encoding="utf-8") as f:
            f.write(out_report.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, final)
    except OSError as e:
        return _error(f"the judge-only report could not be written: {e}", 3)
    print(final)
    return 0
