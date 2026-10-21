"""Owner blind-review preparation (issue #34): packet masking, id mapping, sheet validation,
freezing and the post-freeze comparison. Offline; the sheets here are synthetic test data, not
review decisions."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from insightforge_agent.benchmark import review
from insightforge_agent.benchmark.cases import BenchmarkCase, load_cases
from insightforge_agent.benchmark.cli import main
from tests.benchmark.helpers import case_dict
from tests.paths import REPO_ROOT

REPO = REPO_ROOT
CASES = REPO / "benchmarks" / "evidence" / "cases.jsonl"
COMMITTED = REPO / "benchmarks" / "evidence" / "review"
TODAY = date(2026, 10, 20)


@pytest.fixture(scope="module")
def pilot() -> list[BenchmarkCase]:
    return load_cases(CASES)


def _small() -> list[BenchmarkCase]:
    return [BenchmarkCase.model_validate(case_dict(t, f"x-{i:02d}", critical=(i == 0)))
            for i, t in enumerate(["PAIR", "SUPPORT", "GROUND", "INDEPENDENCE"])]


@pytest.fixture
def prepared(tmp_path):
    cases = _small()
    review.prepare(cases, tmp_path)
    return cases, tmp_path


def _fill(directory: Path, labels: dict[str, str], *, reviewer="Owner Test", on="2026-10-19"):
    sheet = directory / review.SHEET_FILE
    rows = review.read_sheet(sheet)
    for row in rows:
        if row["review_id"] in labels:
            row.update(label=labels[row["review_id"]], reviewer=reviewer, reviewed_on=on)
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _answers(directory: Path) -> dict[str, str]:
    """A valid label for every item (test data only, not meant to be correct)."""
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    return {e.review_id: next(iter(review.LABELS_BY_TYPE[e.case_type])).value for e in mapping}


# ---- packet construction and masking -----------------------------------------------------------

def test_the_packet_covers_all_48_cases_with_neutral_sequential_ids(pilot):
    items = review.build_items(pilot)
    assert [i.review_id for i in items] == [f"R{n:03d}" for n in range(1, 49)]
    assert {i.case_id for i in items} == {c.id for c in pilot}


def test_the_order_is_reproducible_and_not_grouped_by_type_or_label(pilot):
    first = [i.case_id for i in review.build_items(pilot)]
    assert first == [i.case_id for i in review.build_items(list(reversed(pilot)))]
    types = [i.case_type for i in review.build_items(pilot)]
    assert types != sorted(types, key=lambda t: t.value)
    assert len({t for t in types[:8]}) > 1


def test_the_packet_leaks_no_label_rationale_category_tag_id_or_baseline_field(pilot):
    packet = review.render_packet(review.build_items(pilot))
    # the packet legitimately lists every permitted label once per item; strip those lines
    body = "\n".join(l for l in packet.splitlines() if not l.startswith("**Permitted labels:**"))
    body = body.split("## Labeling rubric")[1].split("\n---\n")[1:]
    text = "\n".join(body)
    for case in pilot:
        assert case.id not in packet
        assert case.rationale not in packet
        assert case.primary_category not in packet
        assert not any(f"`{t}`" in text for t in case.tags)
    for word in ("critical", "structured_a", "grounding_failure", "false_merge", "rationale",
                 "proposed", "expected"):
        assert word not in text.lower(), word
    for label in ("SAME_FACT", "DIFFERENT_FACT", "SUPPORTED", "NOT_GROUNDED", "DEPENDENT"):
        assert label not in text  # a label name appears only in the rubric and permitted lists


def test_each_item_gives_type_question_inputs_and_only_its_own_labels(pilot):
    by_id = {i.case_id: i for i in review.build_items(pilot)}
    pair = by_id["p-fm-01"]
    assert "**Judgment type:** Fact comparison" in pair.body
    assert "Norvale Motors sold 4.27 million vehicles in 2024." in pair.body
    assert pair.labels == ["SAME_FACT", "DIFFERENT_FACT", "CONTRADICTORY"]
    assert "4.27 million" in pair.body and "vehicles sold" not in pair.body  # no structured fields
    span = by_id["g-wi-01"].body
    assert "Quoted span" in span and "Passage" in span
    assert "obs-g01" not in span  # passage identity is plumbing
    independence = next(i for i in by_id.values() if i.case_type.value == "INDEPENDENCE")
    assert "Source A URL" in independence.body and "Source B text" in independence.body


def test_the_sheet_template_is_blank_and_carries_no_identity_or_date(pilot):
    rows = review.render_sheet_rows(review.build_items(pilot))
    assert len(rows) == 48
    for row in rows:
        assert not any(row[c] for c in ("label", "uncertain", "note", "reviewer", "reviewed_on"))


def test_the_committed_review_files_match_a_fresh_build_of_the_case_file(pilot):
    items = review.build_items(pilot)
    assert (COMMITTED / review.PACKET_FILE).read_text(encoding="utf-8") == review.render_packet(items)
    # The proposal pin deliberately records the record as it was before review, so after the
    # review integration only the packet content digests are expected to match a fresh build.
    strip = lambda m: [{k: v for k, v in i.items() if k != "proposal_sha256"} for i in m["items"]]  # noqa: E731
    committed = json.loads((COMMITTED / review.MAPPING_FILE).read_text(encoding="utf-8"))
    assert strip(committed) == strip(review.build_mapping(items))
    sheet = review.read_sheet(COMMITTED / review.SHEET_FILE)
    assert [r["review_id"] for r in sheet] == [i.review_id for i in items]


def test_the_pilot_composition_is_unchanged_by_review(pilot):
    assert len(pilot) == 48 and sum(c.critical for c in pilot) == 28


# ---- mapping -----------------------------------------------------------------------------------

def test_the_mapping_is_a_separate_file_from_the_packet_and_maps_ids(prepared):
    cases, directory = prepared
    packet = (directory / review.PACKET_FILE).read_text(encoding="utf-8")
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    assert {e.case_id for e in mapping} == {c.id for c in cases}
    assert not any(e.case_id in packet for e in mapping)
    review.verify_mapping(mapping, cases)


def test_a_case_changed_after_the_packet_was_made_is_detected(prepared):
    cases, directory = prepared
    changed = [BenchmarkCase.model_validate(
        case_dict("PAIR", "x-00", inputs={"statement_a": "Different text.", "statement_b": "B."}))
        ] + cases[1:]
    with pytest.raises(review.ReviewError, match="out of date"):
        review.verify_mapping(review.load_mapping(directory / review.MAPPING_FILE), changed)


# ---- never overwrite ---------------------------------------------------------------------------

def test_prepare_refuses_to_overwrite_an_existing_sheet(prepared):
    cases, directory = prepared
    _fill(directory, _answers(directory))
    before = (directory / review.SHEET_FILE).read_bytes()
    with pytest.raises(review.ReviewError, match="already prepared"):
        review.prepare(cases, directory)
    assert (directory / review.SHEET_FILE).read_bytes() == before


# ---- validation --------------------------------------------------------------------------------

def _validate(directory: Path):
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    return review.validate_sheet(review.read_sheet(directory / review.SHEET_FILE), mapping, TODAY)


def test_a_blank_sheet_is_incomplete_not_invalid(prepared):
    _, directory = prepared
    report = _validate(directory)
    assert report.problems == [] and len(report.pending) == 4 and not report.complete


def test_a_fully_filled_sheet_is_complete(prepared):
    _, directory = prepared
    _fill(directory, _answers(directory))
    report = _validate(directory)
    assert report.complete and len(report.decisions) == 4
    assert {d.reviewed_on for d in report.decisions} == {date(2026, 10, 19)}


def test_a_label_from_another_judgment_type_is_rejected(prepared):
    _, directory = prepared
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    pair = next(e for e in mapping if e.case_type.value == "PAIR")
    _fill(directory, {pair.review_id: "GROUNDED"})
    report = _validate(directory)
    assert any(pair.review_id in p and "not one of" in p for p in report.problems)
    assert not any(d.review_id == pair.review_id for d in report.decisions)


@pytest.mark.parametrize("on, fragment", [
    ("", "YYYY-MM-DD"), ("19/10/2026", "YYYY-MM-DD"), ("2026-10-21", "future")])
def test_a_missing_malformed_or_future_review_date_is_rejected(prepared, on, fragment):
    _, directory = prepared
    _fill(directory, {"R001": _answers(directory)["R001"]}, on=on)
    assert any(fragment in p for p in _validate(directory).problems)


def test_a_missing_reviewer_is_rejected_and_never_filled_in(prepared):
    _, directory = prepared
    _fill(directory, {"R001": _answers(directory)["R001"]}, reviewer="")
    assert any("reviewer is required" in p for p in _validate(directory).problems)


def test_an_uncertain_row_needs_a_note(prepared):
    _, directory = prepared
    _fill(directory, {"R001": _answers(directory)["R001"]})
    rows = review.read_sheet(directory / review.SHEET_FILE)
    rows[0]["uncertain"] = "yes"
    with (directory / review.SHEET_FILE).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    assert any("needs a note" in p for p in _validate(directory).problems)


def test_duplicate_unknown_and_missing_rows_are_reported(prepared):
    _, directory = prepared
    _fill(directory, _answers(directory))
    rows = review.read_sheet(directory / review.SHEET_FILE)
    rows = rows[:3] + [dict(rows[0]), {**rows[1], "review_id": "R999"}]
    with (directory / review.SHEET_FILE).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    report = _validate(directory)
    assert any("R001" in p and "more than once" in p for p in report.problems)
    assert any("R999" in p and "unknown" in p for p in report.problems)
    assert report.pending == ["R004"] and not report.complete


def test_a_sheet_with_the_wrong_columns_is_refused(tmp_path):
    bad = tmp_path / "s.csv"
    bad.write_text("review_id,label\nR001,SAME_FACT\n", encoding="utf-8")
    with pytest.raises(review.ReviewError, match="missing column"):
        review.read_sheet(bad)


# ---- finalize and compare ----------------------------------------------------------------------

def _finalize(directory: Path):
    return review.finalize(_validate(directory), directory / review.SHEET_FILE,
                           directory / review.MAPPING_FILE, directory / review.FROZEN_FILE, TODAY)


def test_finalize_refuses_an_incomplete_sheet(prepared):
    _, directory = prepared
    _fill(directory, {"R001": _answers(directory)["R001"]})
    with pytest.raises(review.ReviewError, match="not complete"):
        _finalize(directory)
    assert not (directory / review.FROZEN_FILE).exists()


def test_finalize_preserves_the_owner_judgment_and_date_and_never_overwrites(prepared):
    _, directory = prepared
    _fill(directory, _answers(directory), reviewer="Owner Test", on="2026-10-18")
    frozen = _finalize(directory)
    record = json.loads(frozen.read_text(encoding="utf-8"))
    assert record["finalized_on"] == "2026-10-20"
    assert all(d["reviewed_on"] == "2026-10-18" and d["reviewer"] == "Owner Test"
               for d in record["decisions"])
    assert "expected" not in frozen.read_text(encoding="utf-8")  # no proposed label stored
    with pytest.raises(review.ReviewError, match="already exists"):
        _finalize(directory)  # a second import cannot replace the first


def test_compare_is_refused_until_the_review_is_finalized(prepared):
    cases, directory = prepared
    _fill(directory, _answers(directory))
    with pytest.raises(review.ReviewError, match="not finalized"):
        review.compare(directory / review.FROZEN_FILE, directory / review.SHEET_FILE,
                       directory / review.MAPPING_FILE, cases)


def test_compare_reports_a_discrepancy_without_changing_either_side(prepared):
    cases, directory = prepared
    answers = _answers(directory)
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    pair = next(e for e in mapping if e.case_type.value == "PAIR")
    answers[pair.review_id] = "DIFFERENT_FACT"  # the helper case is proposed SAME_FACT
    _fill(directory, answers)
    _finalize(directory)
    rows = review.compare(directory / review.FROZEN_FILE, directory / review.SHEET_FILE,
                          directory / review.MAPPING_FILE, cases)
    differing = [r for r in rows if not r.agrees]
    assert [(r.case_id, r.owner_label, r.proposed_label) for r in differing] == \
        [("x-00", "DIFFERENT_FACT", "SAME_FACT")]
    assert differing[0].critical
    assert cases[0].expected.value == "SAME_FACT" and cases[0].label_status.value == "proposed"
    assert "not an independent human judgment" in review.render_comparison(rows)


def test_compare_detects_a_sheet_edited_after_the_freeze(prepared):
    cases, directory = prepared
    _fill(directory, _answers(directory))
    _finalize(directory)
    _fill(directory, {"R001": "CONTRADICTORY"})
    with pytest.raises(review.ReviewError, match="edited after"):
        review.compare(directory / review.FROZEN_FILE, directory / review.SHEET_FILE,
                       directory / review.MAPPING_FILE, cases)


# ---- the CLI -----------------------------------------------------------------------------------

def test_cli_end_to_end_on_a_synthetic_case_file(tmp_path, capsys):
    cases_file = tmp_path / "cases.jsonl"
    cases_file.write_text("\n".join(json.dumps(case_dict(t, f"x-{i:02d}"))
                                    for i, t in enumerate(["PAIR", "SUPPORT"])), encoding="utf-8")
    base = ["--cases", str(cases_file), "--dir", str(tmp_path / "rv")]
    assert main(["review", "prepare", *base]) == 0
    assert main(["review", "prepare", *base]) == 1  # no overwrite
    assert main(["review", "validate", *base]) == 1  # blank: incomplete
    assert main(["review", "validate", "--allow-incomplete", *base]) == 0
    assert main(["review", "finalize", *base]) == 1  # not complete
    assert main(["review", "compare", *base]) == 1  # not finalized
    out = capsys.readouterr().out
    assert "already prepared" in out and "not finalized" in out
    assert "x-00" not in (tmp_path / "rv" / review.PACKET_FILE).read_text(encoding="utf-8")
    sheet = review.read_sheet(tmp_path / "rv" / review.SHEET_FILE)
    assert all(not r["label"] and not r["reviewer"] for r in sheet)


# ---- the frozen record is verified, not trusted ------------------------------------------------

def _frozen_compare(directory: Path, cases):
    return review.compare(directory / review.FROZEN_FILE, directory / review.SHEET_FILE,
                          directory / review.MAPPING_FILE, cases)


def _tamper(directory: Path, change):
    path = directory / review.FROZEN_FILE
    record = json.loads(path.read_text(encoding="utf-8"))
    change(record)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


@pytest.fixture
def frozen(prepared):
    cases, directory = prepared
    _fill(directory, _answers(directory))
    _finalize(directory)
    assert len(_frozen_compare(directory, cases)) == 4  # untouched record compares fine
    return cases, directory


@pytest.mark.parametrize("name, change", [
    ("label", lambda r: r["decisions"][0].update(label="CONTRADICTORY")),
    ("reviewer", lambda r: r["decisions"][1].update(reviewer="Someone Else")),
    ("date", lambda r: r["decisions"][2].update(reviewed_on="2026-10-01")),
    ("note", lambda r: r["decisions"][0].update(note="added later")),
    ("uncertain", lambda r: r["decisions"][0].update(uncertain=True)),
    ("case id", lambda r: r["decisions"][0].update(case_id="x-03")),
    ("missing decision", lambda r: r["decisions"].pop()),
    ("duplicate decision", lambda r: r["decisions"].append(dict(r["decisions"][0]))),
    ("extra decision", lambda r: r["decisions"].append({**r["decisions"][0], "review_id": "R009"})),
    ("finalized_on", lambda r: r.update(finalized_on="2026-10-01")),
    ("extra key", lambda r: r.update(proposed_label="SAME_FACT")),
])
def test_compare_rejects_a_frozen_record_that_differs_from_the_finalized_sheet(frozen, name, change):
    cases, directory = frozen
    _tamper(directory, change)
    with pytest.raises(review.ReviewError, match="does not match|valid|finalized"):
        _frozen_compare(directory, cases)


def test_compare_rejects_a_missing_sheet_a_changed_mapping_and_unreadable_json(frozen):
    cases, directory = frozen
    (directory / review.FROZEN_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(review.ReviewError, match="not valid JSON"):
        _frozen_compare(directory, cases)
    (directory / review.FROZEN_FILE).write_text("[]", encoding="utf-8")
    with pytest.raises(review.ReviewError, match="not a review record"):
        _frozen_compare(directory, cases)
    sheet = directory / review.SHEET_FILE
    sheet.rename(directory / "moved.csv")
    with pytest.raises(review.ReviewError):
        _frozen_compare(directory, cases)


# ---- the proposal is pinned --------------------------------------------------------------------

def _with_expected(cases, case_id, expected):
    return [BenchmarkCase.model_validate({**c.model_dump(mode="json"), "expected": expected})
            if c.id == case_id else c for c in cases]


def test_the_mapping_pins_the_proposed_record_without_revealing_the_label(prepared):
    cases, directory = prepared
    text = (directory / review.MAPPING_FILE).read_text(encoding="utf-8")
    assert "SAME_FACT" not in text and "expected" not in text
    assert all(e.proposal_sha256 for e in review.load_mapping(directory / review.MAPPING_FILE))


@pytest.mark.parametrize("edit", [
    lambda c: _with_expected(c, "x-00", "DIFFERENT_FACT"),
    lambda c: [BenchmarkCase.model_validate({**x.model_dump(mode="json"), "rationale": "new"})
               if x.id == "x-01" else x for x in c],
    lambda c: [BenchmarkCase.model_validate({**x.model_dump(mode="json"), "critical": True})
               if x.id == "x-02" else x for x in c],
])
def test_compare_rejects_a_proposal_changed_after_preparation(frozen, edit):
    cases, directory = frozen
    with pytest.raises(review.ReviewError, match="proposed record"):
        _frozen_compare(directory, edit(cases))


def test_finalize_through_the_cli_rejects_a_proposal_changed_after_preparation(tmp_path, capsys):
    cases_file = tmp_path / "cases.jsonl"
    rows = [case_dict("PAIR", "x-00"), case_dict("SUPPORT", "x-01")]
    cases_file.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    base = ["--cases", str(cases_file), "--dir", str(tmp_path / "rv")]
    assert main(["review", "prepare", *base]) == 0
    answers = _answers(tmp_path / "rv")
    _fill(tmp_path / "rv", answers)
    rows[0]["expected"] = "DIFFERENT_FACT"
    cases_file.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert main(["review", "finalize", *base]) == 1
    assert "proposed record" in capsys.readouterr().out
    assert not (tmp_path / "rv" / review.FROZEN_FILE).exists()


def test_an_old_mapping_without_pins_cannot_be_used_for_comparison(prepared):
    cases, directory = prepared
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    unpinned = [review.MapEntry(e.review_id, e.case_id, e.case_type, e.content_sha256, "")
                for e in mapping]
    with pytest.raises(review.ReviewError, match="never pinned"):
        review.verify_mapping(unpinned, cases, pin_proposals=True)


def test_the_rubric_says_extra_context_can_still_be_the_same_fact():
    assert "extra context" in review.RUBRIC and "contained in it unchanged" in review.RUBRIC
