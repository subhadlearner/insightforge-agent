"""The Stage C coverage rules (tests/t18_acceptance.py) applied to the saved T18 baseline
evaluation: they must fail it for exactly the defects T18 recorded, and must not pass it on its
Gap lines or logged reasons. Read-only; set T18_EVAL_DIR to the saved evaluation directory
(20261019T051042Z, holding eval-report.json and anthropic/eval.db), else skipped."""

import os
from pathlib import Path

import pytest

from tests.t18_acceptance import stage_c

pytestmark = pytest.mark.integration
DIR = os.environ.get("T18_EVAL_DIR")


def test_the_t18_baseline_fails_stage_c_for_the_recorded_defects():
    if not DIR or not (Path(DIR) / "eval-report.json").is_file():
        pytest.skip("T18_EVAL_DIR is not set to the saved T18 evaluation directory")
    result = stage_c(Path(DIR))
    document, regulatory = result["document-annual-report"], result["regulatory-impact-semiconductors"]
    assert document["status"] == "failed"
    assert {f for f, v in document["facts"].items() if v == "covered"} == {
        "revenue 612 million", "growth 14 percent", "NW-200 approval expected H1 2026",
        "fiscal 2026 outlook 9 to 11 percent"}
    assert {f for f, v in document["facts"].items() if v == "missing"} == {
        "cardiology segment 341 million", "orthopaedic segment 187 million",
        "diagnostics segment 84 million", "R&D spending 71 million",
        "6 programmes in clinical trials", "NW-200 pivotal trial",
        "single titanium supplier risk", "hospital pricing pressure risk", "NW-200 delay risk"}
    # The facts were fetched and lost: a pipeline failure, not source unavailability.
    assert regulatory == {"status": "failed", "objectives": {
        "Nvidia China revenue": "pipeline loss", "H20 charge or lost sales": "pipeline loss"}}
