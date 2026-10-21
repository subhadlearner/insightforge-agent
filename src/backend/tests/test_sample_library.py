"""The sample brief library entries are well formed and their documents are reproducible."""

import importlib.util
from pathlib import Path

import yaml

from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.documents import Upload, check_uploads
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT
BRIEFS = sorted((ROOT / "samples" / "briefs").glob("*.yml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_brief_has_the_required_fields():
    assert BRIEFS
    for path in BRIEFS:
        b = load(path)
        assert {"id", "industry", "question_type", "brief", "source_toggles",
                "expected_report_structure"} <= set(b), path.name
        headings = [s["heading"] for s in b["expected_report_structure"]["sections"]]
        assert headings[0] == "Executive Summary" and headings[-1] == "Gaps & limitations"


def test_the_document_brief_is_a_valid_submission_with_its_documents():
    b = load(ROOT / "samples" / "briefs" / "document-annual-report.yml")
    uploads = [Upload(filename=Path(p).name, data=(ROOT / p).read_bytes()) for p in b["documents"]]
    brief = Brief(text=b["brief"], source_toggles=b["source_toggles"],
                  document_ids=[u.document_id for u in uploads])
    check_uploads(brief, uploads)
    assert brief.source_toggles["documents"] and len(brief.text) <= 500


def test_the_memory_brief_has_a_completed_setup_run_and_a_memory_run():
    b = load(ROOT / "samples" / "briefs" / "memory-prior-report.yml")
    setup = b["setup"]
    prior = Brief(text=setup["brief"], source_toggles=setup["source_toggles"])
    brief = Brief(text=b["brief"], source_toggles=b["source_toggles"])
    assert setup["runs_before"] >= 1 and not prior.source_toggles["memory"]
    assert brief.source_toggles["memory"] and not brief.document_ids
    assert len(prior.text) <= 500 and len(brief.text) <= 500
    sections = {s["heading"]: s for s in b["expected_report_structure"]["sections"]}
    assert sections["Earlier findings"]["source_types"] == ["memory"]
    checks = " ".join(b["expected_report_structure"]["checks"])
    assert "historical_web_refetch" in checks and "Evidence bundle" in checks


def test_committed_sample_documents_match_their_generator():
    spec = importlib.util.spec_from_file_location(
        "make_sample_documents", ROOT / "samples" / "documents" / "make_sample_documents.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in module.DOCUMENTS:
        assert (ROOT / "samples" / "documents" / name).read_bytes() == module.render(name), name
