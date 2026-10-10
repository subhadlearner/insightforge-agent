"""Builders for benchmark test data. Fixture cases are test data only; the production case file
(benchmarks/evidence/cases.jsonl) belongs to issue #32."""

import json
from pathlib import Path

from insightforge_agent.benchmark.cases import BenchmarkCase, CaseType

_INPUTS = {
    "PAIR": {"statement_a": "Acme revenue was $5M in 2024.", "statement_b": "Acme earned $5M in 2024."},
    "SUPPORT": {"statement": "Acme revenue was $5M.", "passage": "Acme reported revenue of $5M."},
    "GROUND": {"claim": "Acme grew.", "item_statement": "Acme revenue grew 10%."},
    "INDEPENDENCE": {
        "source_a_url": "https://a.example/x", "source_a_text": "Acme grew.",
        "source_b_url": "https://b.example/y", "source_b_text": "Acme expanded.",
    },
}
_EXPECTED = {
    "PAIR": "SAME_FACT", "SUPPORT": "SUPPORTED", "GROUND": "GROUNDED", "INDEPENDENCE": "INDEPENDENT",
}


def case_dict(case_type: str = "PAIR", case_id: str = "c001", **overrides) -> dict:
    base = {
        "id": case_id, "type": case_type, "primary_category": "numbers", "origin": "synthetic",
        "inputs": dict(_INPUTS[case_type]), "expected": _EXPECTED[case_type],
        "rationale": "Same figure, reworded.",
    }
    base.update(overrides)
    return base


def reviewed(**overrides) -> dict:
    return {"label_status": "reviewed", "reviewer": "alice", "reviewed_at": "2026-10-01", **overrides}


def make_case(case_type: str = "PAIR", case_id: str = "c001", **overrides) -> BenchmarkCase:
    return BenchmarkCase.model_validate(case_dict(case_type, case_id, **overrides))


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


__all__ = ["CaseType", "case_dict", "make_case", "reviewed", "write_jsonl"]
