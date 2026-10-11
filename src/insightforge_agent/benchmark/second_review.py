"""Independent second-human review of a sample of the pilot cases (M1.5, issue #34).

Preparation only: a deterministic stratified sample taken from the owner's FROZEN original blind
review (never the adjudication or the proposed labels' disagreements), a standalone blind
packet and blank sheet, and validate / finalize / verify steps that keep the second reviewer's
judgments apart from everything else. No agreement statistic is computed here.

The second reviewer's files hold no owner label, owner note, case id, category, critical flag or
baseline prediction. The sample manifest and id mapping are sealed from the reviewer."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from insightforge_agent.benchmark import review
from insightforge_agent.benchmark.cases import POSITIVE_LABEL, BenchmarkCase, CaseType
from insightforge_agent.benchmark.review import (
    MapEntry,
    ReviewError,
    ReviewItem,
    SheetReport,
)

SECOND_DIR = Path("benchmarks/evidence/second-review")
PACKET_FILE = "second-review-packet.md"
SHEET_FILE = "second-review-decisions.csv"
MAPPING_FILE = "sealed/second-review-map.json"
FROZEN_FILE = "second-review.frozen.json"

SEED = "insightforge-m1.5-second-review-v1"
ALLOCATION: dict[CaseType, int] = {
    CaseType.PAIR: 4, CaseType.SUPPORT: 2, CaseType.GROUND: 2, CaseType.INDEPENDENCE: 2}
PREFERRED_GAP_DAYS = 7
_FORMAT_VERSION = 1

SELECTION_RULES = (
    "1. Each case is labelled positive (the owner's ORIGINAL frozen label is the type's "
    "positive class) or negative. 2. Per judgment type the allocation is fixed; it is split as "
    "evenly as possible between positive and negative (positive gets the smaller half), moving "
    "the shortfall to the other stratum if one is too small. 3. Within a stratum, critical "
    "cases come first, then cases ordered by sha256(seed + case id); the first k are taken. "
    "4. Nothing else is read: no adjudication, no proposed-label disagreement, no baseline "
    "prediction. 5. Reviewer order is sha256(seed + 'order:' + case id), independent of stratum."
)

INTRO = """\
You are the **independent second reviewer**. Please judge these {n} items yourself, on your own,
without asking anyone for help and without looking at any other record of these cases.

**What to do**
1. Read the labeling rubric below.
2. Read each item (S001 to S{n:03d}) and choose one of its permitted labels.
3. Open the spreadsheet `second-review-decisions.csv` (Excel, Google Sheets, Numbers or any text
   editor). For each row fill in:
   - `label`: your chosen label, exactly as written (for example `DIFFERENT_FACT`);
   - `reviewer`: your full name, as you want it recorded;
   - `reviewed_on`: the real date you made the judgment, as `YYYY-MM-DD` (for example
     `2026-10-19`). If your spreadsheet changes the format, set the column to Text first;
   - `uncertain` (type `yes` if unsure) and `note` (why). Optional, but a `yes` needs a note.
4. Save the file as CSV with the same name and columns, and send it back to the owner.

Do not change `review_id` or `permitted_labels`. Do not leave `label` blank: if you are unsure,
give your best label and mark it uncertain.
"""


@dataclass(frozen=True)
class Selected:
    case: BenchmarkCase
    stratum: str  # "positive" or "negative" under the owner's original label
    owner_review_id: str


def _digest(text: str) -> str:
    return hashlib.sha256((SEED + text).encode("utf-8")).hexdigest()


def select_sample(cases: Sequence[BenchmarkCase], owner_decisions: Sequence[dict[str, Any]]
                  ) -> list[Selected]:
    """Deterministic stratified sample from the owner's frozen original decisions."""
    cases_by_id = {c.id: c for c in cases}
    owner = {d["case_id"]: d for d in owner_decisions}
    missing = sorted(set(cases_by_id) - set(owner))
    if missing:
        raise ReviewError(f"{len(missing)} case(s) have no frozen owner decision")
    chosen: list[Selected] = []
    for case_type, total in ALLOCATION.items():
        pools: dict[str, list[BenchmarkCase]] = {"positive": [], "negative": []}
        for case in cases:
            if case.type is case_type:
                positive = owner[case.id]["label"] == POSITIVE_LABEL[case_type].value
                pools["positive" if positive else "negative"].append(case)
        for pool in pools.values():
            pool.sort(key=lambda c: (not c.critical, _digest(c.id)))
        take = {"positive": total // 2, "negative": total - total // 2}
        for short, other in (("positive", "negative"), ("negative", "positive")):
            if len(pools[short]) < take[short]:
                take[other] += take[short] - len(pools[short])
                take[short] = len(pools[short])
        if take["positive"] + take["negative"] != total or len(pools["negative"]) < take["negative"]:
            raise ReviewError(f"not enough {case_type.value} cases for a sample of {total}")
        for stratum, pool in pools.items():
            chosen += [Selected(c, stratum, owner[c.id]["review_id"]) for c in pool[:take[stratum]]]
    return chosen


def build_second_items(selected: Sequence[Selected]) -> list[ReviewItem]:
    ordered = sorted(selected, key=lambda s: _digest("order:" + s.case.id))
    return [ReviewItem(f"S{n:03d}", s.case.id, s.case.type, review._item_body(s.case),
                       review.proposal_digest(s.case))
            for n, s in enumerate(ordered, start=1)]


def build_second_mapping(selected: Sequence[Selected], items: Sequence[ReviewItem],
                         owner_frozen: Path, owner_mapping: Path) -> dict[str, Any]:
    by_case = {s.case.id: s for s in selected}
    strata: dict[str, dict[str, int]] = {}
    for s in selected:
        strata.setdefault(s.case.type.value, {"positive": 0, "negative": 0})[s.stratum] += 1
    return {
        "format": _FORMAT_VERSION,
        "kind": "second-review-map",
        "note": "Sealed from the second reviewer: it names the cases and their strata. "
                "Contains no owner label.",
        "seed": SEED,
        "selection_rules": SELECTION_RULES,
        "allocation": {t.value: n for t, n in ALLOCATION.items()},
        "strata": strata,
        "owner_frozen_sha256": review._text_digest(owner_frozen),
        "owner_mapping_sha256": review._text_digest(owner_mapping),
        "items": [{"review_id": i.review_id, "case_id": i.case_id, "type": i.case_type.value,
                   "content_sha256": i.digest, "proposal_sha256": i.proposal_sha256,
                   "owner_review_id": by_case[i.case_id].owner_review_id,
                   "stratum": by_case[i.case_id].stratum,
                   "critical": by_case[i.case_id].case.critical} for i in items],
    }


def _load_owner(owner_dir: Path, cases: Sequence[BenchmarkCase]) -> dict[str, Any]:
    """The finalized owner review, verified. Never creates or repairs it."""
    frozen_path = owner_dir / review.FROZEN_FILE
    needed = [frozen_path, owner_dir / review.SHEET_FILE, owner_dir / review.MAPPING_FILE]
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise ReviewError(
            "the finalized owner review is missing: " + ", ".join(missing) + ". The second "
            "review is prepared only from the owner's original frozen review. Restore those "
            "files (they are human judgments and are never regenerated or recreated here), "
            "then rerun.")
    try:
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    except ValueError:
        raise ReviewError(f"{frozen_path} is not valid JSON") from None
    review._verify_frozen(frozen, owner_dir / review.SHEET_FILE, owner_dir / review.MAPPING_FILE)
    review.verify_mapping(review.load_mapping(owner_dir / review.MAPPING_FILE), cases)
    return frozen


def prepare_second(cases: Sequence[BenchmarkCase], owner_dir: Path, second_dir: Path
                   ) -> list[Path]:
    """Write the blind packet, the blank sheet and the sealed manifest/mapping. Refuses to
    overwrite anything, so returned work can never be regenerated over."""
    owner = _load_owner(owner_dir, cases)
    targets = [second_dir / PACKET_FILE, second_dir / SHEET_FILE, second_dir / MAPPING_FILE]
    existing = [t for t in targets if t.exists()]
    if existing:
        raise ReviewError("second review already prepared: " + ", ".join(str(t) for t in existing)
                          + " (delete them deliberately to start again)")
    selected = select_sample(cases, owner["decisions"])
    items = build_second_items(selected)
    mapping = build_second_mapping(selected, items, owner_dir / review.FROZEN_FILE,
                                   owner_dir / review.MAPPING_FILE)
    review._create(targets[0], review.render_packet(
        items, title="Independent second review packet", intro=INTRO.format(n=len(items))))
    review._create(targets[1], review._csv_text(review.render_sheet_rows(items)), newline="",
                   encoding="utf-8-sig")
    review._create(targets[2], review.mapping_text(mapping))
    return targets


# ---- validate: the generic sheet checks plus the second-reviewer rules ----------------------

def _same_person(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def validate_second(rows: Sequence[dict[str, str]], mapping: Sequence[MapEntry],
                    owner_frozen: dict[str, Any], today: date) -> SheetReport:
    base = review.validate_sheet(rows, mapping, today)
    problems = list(base.problems)
    owner = {d["case_id"]: d for d in owner_frozen["decisions"]}
    names = {d.reviewer for d in base.decisions}
    if len({" ".join(n.casefold().split()) for n in names}) > 1:
        problems.append("more than one reviewer name on the sheet: " + ", ".join(sorted(names))
                        + " (one person must complete it)")
    for d in base.decisions:
        theirs = owner[d.case_id]
        if _same_person(d.reviewer, theirs["reviewer"]):
            problems.append(f"{d.review_id}: the second reviewer must differ from the owner "
                            f"reviewer {theirs['reviewer']!r}")
        if d.reviewed_on < date.fromisoformat(theirs["reviewed_on"]):
            problems.append(f"{d.review_id}: reviewed_on {d.reviewed_on} is before the owner's "
                            f"review on {theirs['reviewed_on']}")
    return SheetReport(base.decisions, base.pending, problems)


# ---- finalize and verify -------------------------------------------------------------------

def _record(report: SheetReport, owner_frozen: dict[str, Any], sheet_path: Path,
            mapping_path: Path, owner_frozen_path: Path, finalized_on: date) -> dict[str, Any]:
    owner = {d["case_id"]: d for d in owner_frozen["decisions"]}
    decisions = []
    for d in sorted(report.decisions, key=lambda d: d.review_id):
        owner_date = date.fromisoformat(owner[d.case_id]["reviewed_on"])
        decisions.append({
            "review_id": d.review_id, "case_id": d.case_id, "type": d.case_type.value,
            "label": d.label, "uncertain": d.uncertain, "note": d.note, "reviewer": d.reviewer,
            "reviewed_on": d.reviewed_on.isoformat(),
            "owner_reviewed_on": owner_date.isoformat(),
            "interval_days": (d.reviewed_on - owner_date).days,
        })
    gaps = [x["interval_days"] for x in decisions]
    return {
        "format": _FORMAT_VERSION,
        "kind": "second-human-review",
        "finalized_on": finalized_on.isoformat(),  # when this freeze ran; not a review date
        "preferred_gap_days": PREFERRED_GAP_DAYS,
        "interval_days_min": min(gaps), "interval_days_max": max(gaps),
        "under_preferred_gap": sum(g < PREFERRED_GAP_DAYS for g in gaps),
        "sheet_sha256": review._text_digest(sheet_path),
        "mapping_sha256": review._text_digest(mapping_path),
        "owner_frozen_sha256": review._text_digest(owner_frozen_path),
        "decisions": decisions,
    }


def _check_inputs(second_dir: Path, owner_dir: Path, cases: Sequence[BenchmarkCase]
                  ) -> tuple[dict[str, Any], list[MapEntry], dict[str, Any]]:
    owner = _load_owner(owner_dir, cases)
    mapping_path = second_dir / MAPPING_FILE
    mapping = review.load_mapping(mapping_path)
    raw = json.loads(mapping_path.read_text(encoding="utf-8"))
    if raw.get("owner_frozen_sha256") != review._text_digest(owner_dir / review.FROZEN_FILE):
        raise ReviewError("the owner's frozen review changed since the sample was drawn")
    review.verify_mapping(mapping, cases, allow_subset=True)
    return owner, mapping, raw


def validate_second_sheet(second_dir: Path, owner_dir: Path, cases: Sequence[BenchmarkCase],
                          today: date) -> SheetReport:
    owner, mapping, _ = _check_inputs(second_dir, owner_dir, cases)
    return validate_second(review.read_sheet(second_dir / SHEET_FILE), mapping, owner, today)


def finalize_second(second_dir: Path, owner_dir: Path, cases: Sequence[BenchmarkCase],
                    today: date) -> Path:
    """Freeze the second reviewer's judgments. Needs a complete, valid sheet; never overwrites.
    The record holds no owner label and no proposed label."""
    owner, mapping, _ = _check_inputs(second_dir, owner_dir, cases)
    report = validate_second(review.read_sheet(second_dir / SHEET_FILE), mapping, owner, today)
    if report.problems or report.pending:
        raise ReviewError("the second-review sheet is not complete and valid; run "
                          "`bench review second-validate`")
    frozen_path = second_dir / FROZEN_FILE
    record = _record(report, owner, second_dir / SHEET_FILE, second_dir / MAPPING_FILE,
                     owner_dir / review.FROZEN_FILE, today)
    review._create(frozen_path, json.dumps(record, indent=2) + "\n")
    return frozen_path


def verify_second(second_dir: Path, owner_dir: Path, cases: Sequence[BenchmarkCase]) -> dict[str, Any]:
    """Read-only. The frozen second review must equal, field for field, what freezing the
    original sheet produces; any altered, missing, extra or duplicate decision is rejected."""
    frozen_path = second_dir / FROZEN_FILE
    if not frozen_path.exists():
        raise ReviewError(f"{frozen_path} not found: the second review is not finalized")
    try:
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    except ValueError:
        raise ReviewError(f"{frozen_path} is not valid JSON") from None
    if not isinstance(frozen, dict):
        raise ReviewError("the frozen file is not a review record")
    try:
        finalized_on = date.fromisoformat(str(frozen.get("finalized_on")))
    except ValueError:
        raise ReviewError("the frozen record has no valid finalized_on date") from None
    owner, mapping, _ = _check_inputs(second_dir, owner_dir, cases)
    sheet_path = second_dir / SHEET_FILE
    if frozen.get("sheet_sha256") != review._text_digest(sheet_path):
        raise ReviewError("the second-review sheet was edited after it was finalized; the frozen "
                          "record is the original judgment")
    report = validate_second(review.read_sheet(sheet_path), mapping, owner, finalized_on)
    if report.problems or report.pending:
        raise ReviewError("the finalized sheet is not a complete valid sheet: "
                          + "; ".join(report.problems + report.pending))
    expected = _record(report, owner, sheet_path, second_dir / MAPPING_FILE,
                       owner_dir / review.FROZEN_FILE, finalized_on)
    if frozen != expected:
        raise ReviewError("the frozen second review does not match the finalized sheet, mapping "
                          "and owner record (a label, reviewer, date or decision was altered, "
                          "missing or duplicated)")
    return frozen
