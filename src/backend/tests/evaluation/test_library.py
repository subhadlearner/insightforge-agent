"""The sample brief library (T16 #17): five briefs across industries and question types, each
loaded into the production Brief contract with its expected Report structure."""

from pathlib import Path

import pytest

from insightforge_agent.evaluation.library import (
    LibraryError,
    library_fingerprint,
    load_library,
)
from tests.paths import REPO_ROOT

ROOT = REPO_ROOT
LIBRARY = ROOT / "samples" / "briefs"

WEB_BRIEF = """\
id: {id}
industry: {industry}
question_type: {question_type}
brief: A question about something.
source_toggles: {{web: true}}
expected_report_structure:
  sections:
    - heading: Executive Summary
    - heading: Findings
    - heading: Gaps & limitations
"""


def write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text, encoding="utf-8")


def test_the_library_holds_five_briefs_across_industries_and_question_types():
    samples = load_library(LIBRARY, ROOT)
    assert len(samples) == 5
    assert len({s.industry for s in samples}) == 5
    assert len({s.question_type for s in samples}) == 5
    # The document and memory briefs from T7 and T8 are part of it, unchanged.
    assert {"document-annual-report", "memory-prior-report"} <= {s.id for s in samples}


def test_every_brief_is_a_valid_production_brief_with_its_uploads():
    for sample in load_library(LIBRARY, ROOT):
        uploads = sample.uploads(ROOT)
        brief = sample.to_brief(uploads)
        assert brief.text == sample.brief
        assert brief.document_ids == [u.document_id for u in uploads]
        assert bool(uploads) == bool(brief.source_toggles.get("documents"))


def test_the_memory_brief_carries_its_setup_run_and_the_others_do_not():
    by_id = {s.id: s for s in load_library(LIBRARY, ROOT)}
    memory = by_id.pop("memory-prior-report")
    assert memory.setup is not None and memory.setup.runs_before == 1
    assert not memory.setup.to_brief().source_toggles.get("memory")
    assert all(s.setup is None for s in by_id.values())


def test_a_library_that_is_not_five_briefs_is_rejected(tmp_path):
    for n in range(4):
        write(tmp_path, f"b{n}.yml", WEB_BRIEF.format(id=f"b{n}", industry=f"i{n}",
                                                      question_type=f"q{n}"))
    with pytest.raises(LibraryError, match="5 sample briefs"):
        load_library(tmp_path, ROOT)


def test_duplicate_ids_are_rejected(tmp_path):
    for n in range(5):
        write(tmp_path, f"b{n}.yml", WEB_BRIEF.format(id="same", industry=f"i{n}",
                                                      question_type=f"q{n}"))
    with pytest.raises(LibraryError, match="duplicate"):
        load_library(tmp_path, ROOT)


@pytest.mark.parametrize("text, match", [
    (WEB_BRIEF.replace("source_toggles: {{web: true}}",
                       "source_toggles: {{web: true, documents: true}}"), "documents"),
    (WEB_BRIEF.replace("source_toggles: {{web: true}}",
                       "source_toggles: {{web: true, memory: true}}"), "setup"),
    (WEB_BRIEF.replace("    - heading: Executive Summary\n", ""), "Executive Summary"),
    (WEB_BRIEF.replace("    - heading: Gaps & limitations\n", ""), "Gaps & limitations"),
    (WEB_BRIEF + "unexpected: 1\n", "unexpected"),
])
def test_a_malformed_brief_is_rejected_with_its_file(tmp_path, text, match):
    write(tmp_path, "bad.yml", text.format(id="bad", industry="i", question_type="q"))
    with pytest.raises(LibraryError, match=match) as raised:
        load_library(tmp_path, ROOT, size=1)
    assert "bad.yml" in str(raised.value)


def test_a_missing_document_is_rejected(tmp_path):
    text = WEB_BRIEF.replace("source_toggles: {{web: true}}",
                             "source_toggles: {{web: true, documents: true}}\n"
                             "documents: [samples/documents/nowhere.pdf]")
    write(tmp_path, "doc.yml", text.format(id="doc", industry="i", question_type="q"))
    with pytest.raises(LibraryError, match="nowhere.pdf"):
        load_library(tmp_path, ROOT, size=1)


def test_the_fingerprint_changes_with_a_brief_or_a_document(tmp_path):
    write(tmp_path, "b.yml", WEB_BRIEF.format(id="b", industry="i", question_type="q"))
    samples = load_library(tmp_path, ROOT, size=1)
    before = library_fingerprint(tmp_path, samples, ROOT)
    assert before == library_fingerprint(tmp_path, samples, ROOT)
    write(tmp_path, "b.yml", WEB_BRIEF.format(id="b", industry="i", question_type="q2"))
    assert library_fingerprint(tmp_path, load_library(tmp_path, ROOT, size=1), ROOT) != before
