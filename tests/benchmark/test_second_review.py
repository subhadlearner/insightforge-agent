"""Second-human review preparation (issue #34): sampling from the frozen owner review, blind
materials, second-reviewer rules, freezing and verification. Offline. The owner and second
decisions here are synthetic test data, not review judgments."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from insightforge_agent.benchmark import review, second_review
from insightforge_agent.benchmark.cases import BenchmarkCase, CaseType
from insightforge_agent.benchmark.cli import main
from tests.benchmark.helpers import case_dict

REPO = Path(__file__).resolve().parents[2]
COMMITTED = REPO / "benchmarks" / "evidence" / "second-review"
OWNER_COMMITTED = REPO / "benchmarks" / "evidence" / "review"
TODAY = date(2026, 10, 20)
NEGATIVE = {"PAIR": "DIFFERENT_FACT", "SUPPORT": "INSUFFICIENT_EVIDENCE",
            "GROUND": "NOT_GROUNDED", "INDEPENDENCE": "DEPENDENT"}
POSITIVE = {"PAIR": "SAME_FACT", "SUPPORT": "SUPPORTED", "GROUND": "GROUNDED",
            "INDEPENDENCE": "INDEPENDENT"}


def _cases(flip_expected: bool = False) -> list[BenchmarkCase]:
    out = []
    for type_name in POSITIVE:
        for n in range(8):
            positive = n < 4
            label = (POSITIVE if positive else NEGATIVE)[type_name]
            if flip_expected:  # a different PROPOSAL; the owner's labels stay the same
                label = (NEGATIVE if positive else POSITIVE)[type_name]
            inputs = case_dict(type_name)["inputs"]
            key = {"PAIR": "statement_b", "SUPPORT": "statement", "GROUND": "claim",
                   "INDEPENDENCE": "source_b_text"}[type_name]
            inputs[key] = f"{inputs[key]} variant {n}"
            out.append(BenchmarkCase.model_validate(case_dict(
                type_name, f"{type_name[:2].lower()}-{n:02d}", inputs=inputs, expected=label,
                critical=(not positive and n < 6))))
    return out


def _owner_review(directory: Path, cases, *, notes="", reviewer="Owner Test"):
    """A finalized owner review whose labels follow the original (non-flipped) pilot."""
    review.prepare(cases, directory)
    mapping = review.load_mapping(directory / review.MAPPING_FILE)
    sheet = directory / review.SHEET_FILE
    by_id = {c.id: c for c in _cases()}
    rows = review.read_sheet(sheet)
    for row, entry in zip(rows, mapping):
        row.update(label=(POSITIVE if int(entry.case_id[-2:]) < 4 else NEGATIVE)[entry.case_type.value],
                   reviewer=reviewer, reviewed_on="2026-10-01",
                   uncertain="yes" if notes else "", note=notes)
    assert by_id
    _write(sheet, rows)
    report = review.validate_sheet(review.read_sheet(sheet), mapping, TODAY)
    review.finalize(report, sheet, directory / review.MAPPING_FILE,
                    directory / review.FROZEN_FILE, TODAY)


def _write(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=review.SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def env(tmp_path):
    cases = _cases()
    owner = tmp_path / "owner"
    _owner_review(owner, cases, notes="SECRET-OWNER-NOTE")
    return cases, owner, tmp_path / "second"


@pytest.fixture
def prepared(env):
    cases, owner, second = env
    second_review.prepare_second(cases, owner, second)
    return cases, owner, second


def _mapping(second: Path) -> dict:
    return json.loads((second / second_review.MAPPING_FILE).read_text(encoding="utf-8"))


def _fill(second: Path, *, reviewer="Second Person", on="2026-10-12", only=None, label=None):
    sheet = second / second_review.SHEET_FILE
    types = {i["review_id"]: i["type"] for i in _mapping(second)["items"]}
    rows = review.read_sheet(sheet)
    for row in rows:
        if only is None or row["review_id"] in only:
            row.update(label=label or POSITIVE[types[row["review_id"]]], reviewer=reviewer,
                       reviewed_on=on)
    _write(sheet, rows)


# ---- preconditions -----------------------------------------------------------------------------

def test_a_missing_owner_review_stops_with_an_actionable_error_and_creates_nothing(tmp_path):
    with pytest.raises(review.ReviewError, match="never regenerated or recreated"):
        second_review.prepare_second(_cases(), tmp_path / "owner", tmp_path / "second")
    assert not (tmp_path / "second").exists() and not (tmp_path / "owner").exists()


def test_a_modified_owner_record_is_rejected(env):
    cases, owner, second = env
    path = owner / review.FROZEN_FILE
    record = json.loads(path.read_text(encoding="utf-8"))
    record["decisions"][0]["label"] = "CONTRADICTORY"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(review.ReviewError, match="does not match"):
        second_review.prepare_second(cases, owner, second)
    assert not second.exists()


# ---- sampling ----------------------------------------------------------------------------------

def test_the_sample_has_the_agreed_allocation_and_strata(prepared):
    _, _, second = prepared
    items = _mapping(second)["items"]
    assert len(items) == 10
    counts = {(i["type"], i["stratum"]): 0 for i in items}
    for i in items:
        counts[(i["type"], i["stratum"])] += 1
    assert counts == {("PAIR", "positive"): 2, ("PAIR", "negative"): 2,
                      ("SUPPORT", "positive"): 1, ("SUPPORT", "negative"): 1,
                      ("GROUND", "positive"): 1, ("GROUND", "negative"): 1,
                      ("INDEPENDENCE", "positive"): 1, ("INDEPENDENCE", "negative"): 1}


def test_critical_cases_are_preferred_within_a_stratum(prepared):
    _, _, second = prepared
    negatives = [i for i in _mapping(second)["items"] if i["stratum"] == "negative"]
    assert all(i["critical"] for i in negatives)  # six critical negatives per type were available


def test_the_sample_and_packet_are_reproducible_and_independent_of_case_order(env, tmp_path):
    cases, owner, second = env
    second_review.prepare_second(cases, owner, second)
    second_review.prepare_second(list(reversed(cases)), owner, tmp_path / "again")
    for name in (second_review.PACKET_FILE, second_review.MAPPING_FILE):
        assert (second / name).read_text(encoding="utf-8") == \
            (tmp_path / "again" / name).read_text(encoding="utf-8")


def test_selection_ignores_the_proposed_labels(env, tmp_path):
    cases, owner, second = env
    second_review.prepare_second(cases, owner, second)
    flipped = _cases(flip_expected=True)  # proposals now disagree with the owner everywhere
    second_review.prepare_second(flipped, owner, tmp_path / "flipped")
    chosen = lambda d: [(i["case_id"], i["review_id"]) for i in _mapping(d)["items"]]  # noqa: E731
    assert chosen(second) == chosen(tmp_path / "flipped")


def test_selection_follows_the_owners_frozen_labels(env):
    cases, owner, _ = env
    frozen = json.loads((owner / review.FROZEN_FILE).read_text(encoding="utf-8"))["decisions"]
    picked = {s.case.id: s.stratum for s in second_review.select_sample(cases, frozen)}
    by_owner = {d["case_id"]: d["label"] for d in frozen}
    for case_id, stratum in picked.items():
        assert (by_owner[case_id] in POSITIVE.values()) == (stratum == "positive")


# ---- blind materials ---------------------------------------------------------------------------

def test_second_materials_leak_nothing_from_the_owner_or_the_repository(prepared):
    cases, _, second = prepared
    text = "\n".join((second / n).read_text(encoding="utf-8-sig")
                     for n in (second_review.PACKET_FILE, second_review.SHEET_FILE))
    assert "SECRET-OWNER-NOTE" not in text and "Owner Test" not in text
    for case in cases:
        assert case.id not in text and case.primary_category not in text
    import re
    assert not re.search(r"\bR\d{3}\b", text)  # no owner review id
    for word in ("critical", "stratum", "proposed", "adjudicat", "baseline", "2026-10-01"):
        assert word not in text.lower(), word


def test_new_neutral_ids_and_a_blank_sheet(prepared):
    _, _, second = prepared
    rows = review.read_sheet(second / second_review.SHEET_FILE)
    assert [r["review_id"] for r in rows] == [f"S{n:03d}" for n in range(1, 11)]
    assert all(not r[c] for r in rows for c in ("label", "uncertain", "note", "reviewer",
                                                "reviewed_on"))
    packet = (second / second_review.PACKET_FILE).read_text(encoding="utf-8")
    assert packet.count("\n## S0") == 10 and "## Labeling rubric" in packet


def test_the_mapping_names_cases_but_holds_no_owner_label(prepared):
    _, _, second = prepared
    text = (second / second_review.MAPPING_FILE).read_text(encoding="utf-8")
    assert "SECRET-OWNER-NOTE" not in text and "Owner Test" not in text
    assert "SAME_FACT" not in text and '"label"' not in text


def test_prepare_refuses_to_overwrite_returned_work(prepared):
    cases, owner, second = prepared
    _fill(second)
    before = (second / second_review.SHEET_FILE).read_bytes()
    with pytest.raises(review.ReviewError, match="already prepared"):
        second_review.prepare_second(cases, owner, second)
    assert (second / second_review.SHEET_FILE).read_bytes() == before


# ---- validation --------------------------------------------------------------------------------

def _validate(env_):
    cases, owner, second = env_
    return second_review.validate_second_sheet(second, owner, cases, TODAY)


def test_a_blank_sheet_is_incomplete_and_a_filled_one_complete(prepared):
    assert not _validate(prepared).complete and _validate(prepared).problems == []
    _fill(prepared[2])
    assert _validate(prepared).complete


@pytest.mark.parametrize("name", ["Owner Test", "  owner   TEST ", "OWNER test"])
def test_the_second_reviewer_must_differ_from_the_owner(prepared, name):
    _fill(prepared[2], reviewer=name)
    assert any("must differ" in p for p in _validate(prepared).problems)


def test_one_person_must_complete_the_whole_sheet(prepared):
    _fill(prepared[2], only={"S001"}, reviewer="Person One")
    _fill(prepared[2], only={f"S{n:03d}" for n in range(2, 11)}, reviewer="Person Two")
    assert any("more than one reviewer" in p for p in _validate(prepared).problems)


def test_a_review_dated_before_the_owner_review_is_rejected(prepared):
    _fill(prepared[2], on="2026-09-30")
    assert any("before the owner" in p for p in _validate(prepared).problems)


def test_wrong_label_future_date_and_missing_rows_are_rejected(prepared):
    _fill(prepared[2], only={"S001"}, label="GROUNDED_X")
    _fill(prepared[2], only={"S002"}, on="2026-10-25")
    report = _validate(prepared)
    assert any("S001" in p and "not one of" in p for p in report.problems)
    assert any("S002" in p and "future" in p for p in report.problems)
    assert len(report.pending) == 8


# ---- finalize and verify -----------------------------------------------------------------------

def _finalize(env_):
    cases, owner, second = env_
    return second_review.finalize_second(second, owner, cases, TODAY)


def test_finalize_refuses_an_incomplete_or_invalid_sheet(prepared):
    with pytest.raises(review.ReviewError, match="not complete"):
        _finalize(prepared)
    _fill(prepared[2], reviewer="Owner Test")
    with pytest.raises(review.ReviewError, match="not complete"):
        _finalize(prepared)
    assert not (prepared[2] / second_review.FROZEN_FILE).exists()


def test_finalize_records_the_judgments_dates_and_actual_interval(prepared):
    _fill(prepared[2], on="2026-10-12")
    path = _finalize(prepared)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["kind"] == "second-human-review" and len(record["decisions"]) == 10
    assert all(d["reviewer"] == "Second Person" and d["reviewed_on"] == "2026-10-12"
               and d["owner_reviewed_on"] == "2026-10-01" and d["interval_days"] == 11
               for d in record["decisions"])
    assert record["interval_days_min"] == record["interval_days_max"] == 11
    assert record["under_preferred_gap"] == 0
    assert "SECRET-OWNER-NOTE" not in path.read_text(encoding="utf-8")
    assert second_review.verify_second(second_dir := prepared[2], prepared[1], prepared[0])
    assert second_dir


def test_a_gap_under_seven_days_is_recorded_not_hidden(prepared):
    _fill(prepared[2], on="2026-10-03")
    record = json.loads(_finalize(prepared).read_text(encoding="utf-8"))
    assert record["interval_days_max"] == 2 and record["under_preferred_gap"] == 10


def test_a_finalized_second_review_is_never_overwritten(prepared):
    _fill(prepared[2])
    _finalize(prepared)
    with pytest.raises(review.ReviewError, match="already exists"):
        _finalize(prepared)


@pytest.mark.parametrize("change", [
    lambda r: r["decisions"][0].update(label="CONTRADICTORY"),
    lambda r: r["decisions"][1].update(reviewer="Someone Else"),
    lambda r: r["decisions"][2].update(reviewed_on="2026-10-02"),
    lambda r: r["decisions"][3].update(interval_days=99),
    lambda r: r["decisions"].pop(),
    lambda r: r["decisions"].append(dict(r["decisions"][0])),
    lambda r: r.update(under_preferred_gap=7),
    lambda r: r.update(proposed_label="SAME_FACT"),
])
def test_verify_rejects_a_modified_frozen_second_review(prepared, change):
    cases, owner, second = prepared
    _fill(second)
    path = _finalize(prepared)
    record = json.loads(path.read_text(encoding="utf-8"))
    change(record)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    with pytest.raises(review.ReviewError):
        second_review.verify_second(second, owner, cases)


def test_verify_rejects_an_edited_sheet_or_a_changed_owner_record(prepared):
    cases, owner, second = prepared
    _fill(second)
    _finalize(prepared)
    _fill(second, only={"S001"}, label="DIFFERENT_FACT")
    with pytest.raises(review.ReviewError, match="edited after"):
        second_review.verify_second(second, owner, cases)


def test_the_frozen_second_review_is_unreadable_until_it_exists(prepared):
    cases, owner, second = prepared
    with pytest.raises(review.ReviewError, match="not finalized"):
        second_review.verify_second(second, owner, cases)


# ---- CLI ---------------------------------------------------------------------------------------

def test_cli_second_prepare_without_the_owner_review_fails_clearly(tmp_path, capsys):
    cases_file = tmp_path / "cases.jsonl"
    cases_file.write_text("\n".join(json.dumps(case_dict("PAIR", f"x-{n:02d}"))
                                    for n in range(2)), encoding="utf-8")
    args = ["--cases", str(cases_file), "--dir", str(tmp_path / "none"),
            "--second-dir", str(tmp_path / "second")]
    assert main(["review", "second-prepare", *args]) == 1
    assert "owner review is missing" in capsys.readouterr().out
    assert not (tmp_path / "second").exists()


def test_cli_end_to_end(env, capsys):
    cases, owner, second = env
    cases_file = second.parent / "cases.jsonl"
    cases_file.write_text("\n".join(c.model_dump_json(exclude_defaults=True) for c in cases),
                          encoding="utf-8")
    base = ["--cases", str(cases_file), "--dir", str(owner), "--second-dir", str(second)]
    assert main(["review", "second-prepare", *base]) == 0
    assert main(["review", "second-validate", *base]) == 1
    assert main(["review", "second-validate", "--allow-incomplete", *base]) == 0
    assert main(["review", "second-finalize", *base]) == 1
    _fill(second, on=date.today().isoformat())
    assert main(["review", "second-validate", *base]) == 0
    assert main(["review", "second-finalize", *base]) == 0
    assert main(["review", "second-verify", *base]) == 0
    assert main(["review", "second-finalize", *base]) == 1  # no overwrite
    assert "under the preferred" in capsys.readouterr().out


# ---- the committed materials -------------------------------------------------------------------

def test_the_committed_second_review_materials_are_blind_and_well_formed():
    packet = (COMMITTED / second_review.PACKET_FILE).read_text(encoding="utf-8")
    mapping = json.loads((COMMITTED / second_review.MAPPING_FILE).read_text(encoding="utf-8"))
    sheet = review.read_sheet(COMMITTED / second_review.SHEET_FILE)
    assert [r["review_id"] for r in sheet] == [f"S{n:03d}" for n in range(1, 11)]
    assert all(not r["label"] and not r["reviewer"] and not r["reviewed_on"] for r in sheet)
    assert packet.count("\n## S0") == 10
    assert mapping["seed"] == second_review.SEED
    assert mapping["allocation"] == {t.value: n for t, n in second_review.ALLOCATION.items()}
    for item in mapping["items"]:
        assert item["case_id"] not in packet and item["owner_review_id"] not in packet
    assert "label" not in json.dumps(mapping["items"]).replace("proposal_sha256", "")


def test_the_committed_sample_regenerates_when_the_owner_record_is_present(tmp_path):
    needed = [OWNER_COMMITTED / review.FROZEN_FILE, OWNER_COMMITTED / review.SHEET_FILE]
    if not all(p.exists() for p in needed):
        pytest.skip("the owner's finalized review is not in this checkout")
    from insightforge_agent.benchmark.cases import load_cases
    cases = load_cases(REPO / "benchmarks" / "evidence" / "cases.jsonl")
    second_review.prepare_second(cases, OWNER_COMMITTED, tmp_path)
    for name in (second_review.PACKET_FILE, second_review.MAPPING_FILE):
        assert (tmp_path / name).read_text(encoding="utf-8") == \
            (COMMITTED / name).read_text(encoding="utf-8")


def test_the_pilot_cases_and_owner_records_are_untouched_by_preparation():
    from insightforge_agent.benchmark.cases import load_cases
    cases = load_cases(REPO / "benchmarks" / "evidence" / "cases.jsonl")
    assert len(cases) == 48 and {c.label_status.value for c in cases} == {"proposed"}
    assert CaseType.PAIR in {c.type for c in cases}
