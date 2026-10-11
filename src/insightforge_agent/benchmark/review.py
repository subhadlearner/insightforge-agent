"""Offline owner label review (M1.5, issue #34): a blind packet, a decision sheet, validation,
a frozen record of the owner's independent judgments, and a comparison that only runs after the
freeze. Standard library only; no model is called and no benchmark case is modified.

The reviewer-facing files never contain a proposed label, rationale, critical flag, category,
tag, baseline-only field or case id. The neutral id -> case id mapping lives apart from them."""

import csv
import hashlib
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from insightforge_agent.benchmark.cases import (
    LABELS_BY_TYPE,
    BenchmarkCase,
    CaseType,
    GroundCheck,
    GroundInputs,
    IndependenceInputs,
    PairInputs,
    SupportInputs,
)

REVIEW_DIR = Path("benchmarks/evidence/review")
PACKET_FILE = "owner-review-packet.md"
SHEET_FILE = "owner-decisions.csv"
MAPPING_FILE = "sealed/review-id-map.json"
FROZEN_FILE = "owner-decisions.frozen.json"

ORDER_SALT = "insightforge-m1.5-owner-review-v1:"  # fixed, so the order is reproducible
SHEET_COLUMNS = ["review_id", "permitted_labels", "label", "uncertain", "note", "reviewer",
                 "reviewed_on"]
_FORMAT_VERSION = 1


class ReviewError(Exception):
    """A review step was refused; the message says why and what to do."""


# ---- blind items -------------------------------------------------------------------------------

JUDGMENT_TITLE = {
    CaseType.PAIR: "Fact comparison",
    CaseType.SUPPORT: "Passage support",
    CaseType.GROUND: "Grounding",
    CaseType.INDEPENDENCE: "Source independence",
}

QUESTION = {
    CaseType.PAIR: "Do statements A and B state the same fact, different facts, or "
                   "contradictory facts?",
    CaseType.SUPPORT: "Does the Passage support, contradict, or say too little about the "
                      "statement?",
    CaseType.GROUND: "Is the statement grounded in the Passage?",
    CaseType.INDEPENDENCE: "Are Source A and Source B independent of each other?",
}

RUBRIC = """\
Read each item only for what it shows. Relative dates ("last quarter", "this year") are resolved
against the date of the text they appear in. Judge the meaning, not the wording.

**Fact comparison**
- `SAME_FACT`: one fact, worded, rounded or ordered differently. Both statements are true or
  false together.
- `DIFFERENT_FACT`: they concern different things (measure, period, scope, entity or
  relationship). Both can be true.
- `CONTRADICTORY`: same entity, measure, period and scope, but they cannot both be true
  (opposite polarity or direction, different values of a single-valued predicate, a factor of
  1,000 apart).

**Passage support**
- `SUPPORTED`: the Passage states what the statement says, at the statement's own precision.
- `CONTRADICTED`: the Passage states a conflicting value for the same entity, measure and period.
- `INSUFFICIENT_EVIDENCE`: the Passage is silent on the statement, only forecasts or recalls
  another period, or is less precise than the statement.

**Grounding**
- `GROUNDED`: the Passage (for a quoted-span item, the quoted span read in the Passage's
  context) says what the statement says, with the right entity, measure, number, unit and period.
- `NOT_GROUNDED`: anything else: the span is absent, says something different, or is about
  another entity, measure, unit or period.

**Source independence** (two Sources are independent only if they have different domains and
neither cites the other)
- `INDEPENDENT`: separate reporting; neither reproduces, cites or derives from the other.
- `DEPENDENT`: one reproduces, syndicates, cites or evidently derives from the other.
- `UNDETERMINED`: the texts do not allow either answer.

If you cannot decide, still pick your best label, mark the row `uncertain` and say why in the
note. Do not leave it blank.
"""


@dataclass(frozen=True)
class ReviewItem:
    review_id: str
    case_id: str
    case_type: CaseType
    body: str  # exactly what the reviewer reads; the digest is taken over it

    @property
    def labels(self) -> list[str]:
        return [label.value for label in LABELS_BY_TYPE[self.case_type]]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _field(name: str, text: str | None) -> list[str]:
    return [f"**{name}**", _quote(text), ""] if text and text.strip() else []


def _inputs_markdown(case: BenchmarkCase) -> list[str]:
    """Only the fields a human needs. Structured baseline fields, passage identity and every
    label-bearing field are deliberately not shown."""
    inputs = case.inputs
    lines: list[str] = []
    if isinstance(inputs, PairInputs):
        lines += _field("Statement A", inputs.statement_a)
        lines += _field("Passage A", inputs.passage_a)
        lines += _field("Statement B", inputs.statement_b)
        lines += _field("Passage B", inputs.passage_b)
    elif isinstance(inputs, SupportInputs):
        lines += _field("Statement", inputs.statement)
        lines += _field("Passage", inputs.passage)
    elif isinstance(inputs, GroundInputs):
        what = ("an Evidence statement and the span quoted for it"
                if inputs.check is GroundCheck.QUOTED_SPAN else "a Report Claim (no quoted span)")
        lines += [f"Checking {what}.", ""]
        lines += _field("Statement", inputs.claim)
        lines += _field("Quoted span", inputs.quoted_span)
        lines += _field("Passage", inputs.passage_text)
        lines += _field("Context (what the Passage is about)", inputs.attribution_context)
        if inputs.historical:
            lines += ["The statement is marked as describing a past (historical) figure.", ""]
    elif isinstance(inputs, IndependenceInputs):
        lines += [f"**Source A URL:** {inputs.source_a_url}", ""]
        lines += _field("Source A text", inputs.source_a_text)
        lines += [f"**Source B URL:** {inputs.source_b_url}", ""]
        lines += _field("Source B text", inputs.source_b_text)
    if case.source_url:
        lines += [f"Source: {case.source_url}", ""]
    return lines


def _item_body(case: BenchmarkCase) -> str:
    lines = [f"**Judgment type:** {JUDGMENT_TITLE[case.type]}", "",
             f"**Question:** {QUESTION[case.type]}", ""]
    lines += _inputs_markdown(case)
    labels = " | ".join(f"`{label.value}`" for label in LABELS_BY_TYPE[case.type])
    lines += [f"**Permitted labels:** {labels}"]
    return "\n".join(lines).rstrip() + "\n"


def review_order(cases: Sequence[BenchmarkCase]) -> list[BenchmarkCase]:
    """Reproducible and label-independent: sorted by a hash of the id, not by type, category
    or label, so position says nothing about the answer."""
    def key(case: BenchmarkCase) -> str:
        return hashlib.sha256((ORDER_SALT + case.id).encode("utf-8")).hexdigest()
    return sorted(cases, key=key)


def build_items(cases: Sequence[BenchmarkCase]) -> list[ReviewItem]:
    ordered = review_order(cases)
    return [ReviewItem(f"R{n:03d}", case.id, case.type, _item_body(case))
            for n, case in enumerate(ordered, start=1)]


def render_packet(items: Sequence[ReviewItem]) -> str:
    head = [
        "# Owner blind review packet",
        "",
        f"{len(items)} items. Record each label in `{REVIEW_DIR.as_posix()}/{SHEET_FILE}`, "
        "not here. This packet shows no proposed label, rationale, category or flag; "
        "do not look them up before you finish.",
        "",
        "## Labeling rubric",
        "",
        RUBRIC,
    ]
    parts = ["\n".join(head)]
    for item in items:
        parts.append(f"---\n\n## {item.review_id}\n\n{item.body}")
    return "\n".join(parts).rstrip() + "\n"


def render_sheet_rows(items: Sequence[ReviewItem]) -> list[dict[str, str]]:
    return [{"review_id": i.review_id, "permitted_labels": "|".join(i.labels), "label": "",
             "uncertain": "", "note": "", "reviewer": "", "reviewed_on": ""} for i in items]


def build_mapping(items: Sequence[ReviewItem]) -> dict[str, Any]:
    return {
        "format": _FORMAT_VERSION,
        "note": "Neutral review id -> case id. Reviewer-hidden: case ids hint at categories. "
                "Open only after the blind review is finalized.",
        "order_salt": ORDER_SALT,
        "items": [{"review_id": i.review_id, "case_id": i.case_id, "type": i.case_type.value,
                   "content_sha256": i.digest} for i in items],
    }


def mapping_text(mapping: dict[str, Any]) -> str:
    return json.dumps(mapping, indent=2) + "\n"


# ---- files: never overwrite --------------------------------------------------------------------

def _create(path: Path, text: str, newline: str = "\n", encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding=encoding, newline=newline) as handle:
            handle.write(text)
    except FileExistsError:
        raise ReviewError(f"{path} already exists; refusing to overwrite it") from None


def _text_digest(path: Path) -> str:
    """Digest of the text with line endings normalised, so a checkout's CRLF/LF does not matter."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def prepare(cases: Sequence[BenchmarkCase], out_dir: Path) -> list[Path]:
    """Write the packet, the blank sheet and the sealed mapping. Refuses if any exists, so a
    sheet with the owner's work in it can never be regenerated over."""
    targets = [out_dir / PACKET_FILE, out_dir / SHEET_FILE, out_dir / MAPPING_FILE]
    existing = [t for t in targets if t.exists()]
    if existing:
        raise ReviewError("already prepared: " + ", ".join(str(t) for t in existing)
                          + " (delete them deliberately to start again)")
    items = build_items(cases)
    _create(targets[0], render_packet(items))
    # utf-8-sig so Excel opens it as UTF-8; the reader accepts either.
    sheet = _csv_text(render_sheet_rows(items))
    _create(targets[1], sheet, newline="", encoding="utf-8-sig")
    _create(targets[2], mapping_text(build_mapping(items)))
    return targets


def _csv_text(rows: Sequence[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SHEET_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


# ---- mapping and sheet -------------------------------------------------------------------------

@dataclass(frozen=True)
class MapEntry:
    review_id: str
    case_id: str
    case_type: CaseType
    content_sha256: str


def load_mapping(path: Path) -> list[MapEntry]:
    if not path.exists():
        raise ReviewError(f"mapping not found: {path} (run `bench review prepare` first)")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [MapEntry(e["review_id"], e["case_id"], CaseType(e["type"]), e["content_sha256"])
            for e in raw["items"]]


def verify_mapping(mapping: Sequence[MapEntry], cases: Sequence[BenchmarkCase]) -> None:
    """The packet the owner read must still describe the cases on disk."""
    by_id = {c.id: c for c in cases}
    problems = []
    for entry in mapping:
        case = by_id.get(entry.case_id)
        if case is None:
            problems.append(f"{entry.review_id}: case {entry.case_id} no longer exists")
        elif case.type is not entry.case_type or \
                hashlib.sha256(_item_body(case).encode("utf-8")).hexdigest() != entry.content_sha256:
            problems.append(f"{entry.review_id}: case {entry.case_id} changed since the packet "
                            "was prepared")
    extra = sorted(set(by_id) - {e.case_id for e in mapping})
    if extra:
        problems.append(f"{len(extra)} case(s) are not in the packet")
    if problems:
        raise ReviewError("the packet is out of date with the case file: " + "; ".join(problems))


@dataclass(frozen=True)
class Decision:
    review_id: str
    case_id: str
    case_type: CaseType
    label: str
    uncertain: bool
    note: str
    reviewer: str
    reviewed_on: date


@dataclass(frozen=True)
class SheetReport:
    decisions: list[Decision]
    pending: list[str]
    problems: list[str]

    @property
    def complete(self) -> bool:
        return not self.problems and not self.pending


def read_sheet(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ReviewError(f"decision sheet not found: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in SHEET_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ReviewError(f"{path}: missing column(s) {', '.join(missing)}; "
                              f"expected {', '.join(SHEET_COLUMNS)}")
        rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]
    return [r for r in rows if any(r.values())]  # tolerate trailing blank rows


def validate_sheet(rows: Sequence[dict[str, str]], mapping: Sequence[MapEntry],
                   today: date) -> SheetReport:
    by_review = {e.review_id: e for e in mapping}
    seen: set[str] = set()
    decisions: list[Decision] = []
    pending: list[str] = []
    problems: list[str] = []
    for row in rows:
        rid = row.get("review_id", "")
        entry = by_review.get(rid)
        if entry is None:
            problems.append(f"{rid or '(blank id)'}: unknown review id")
            continue
        if rid in seen:
            problems.append(f"{rid}: appears more than once")
            continue
        seen.add(rid)
        if not row.get("label"):
            pending.append(rid)
            continue
        label = row["label"].upper()
        permitted = [m.value for m in LABELS_BY_TYPE[entry.case_type]]
        issues = []
        if label not in permitted:
            issues.append(f"label {row['label']!r} is not one of {', '.join(permitted)}")
        uncertain_text = row.get("uncertain", "").lower()
        if uncertain_text not in ("", "yes", "no"):
            issues.append("uncertain must be blank, yes or no")
        if uncertain_text == "yes" and not row.get("note"):
            issues.append("an uncertain row needs a note saying why")
        if not row.get("reviewer"):
            issues.append("reviewer is required")
        reviewed_on = None
        try:
            reviewed_on = date.fromisoformat(row.get("reviewed_on", ""))
            if reviewed_on > today:
                issues.append(f"reviewed_on {reviewed_on} is in the future")
        except ValueError:
            issues.append("reviewed_on must be the actual review date as YYYY-MM-DD")
        if issues:
            problems.extend(f"{rid}: {issue}" for issue in issues)
            continue
        decisions.append(Decision(rid, entry.case_id, entry.case_type, label,
                                  uncertain_text == "yes", row.get("note", ""),
                                  row["reviewer"], reviewed_on))
    pending.extend(sorted(set(by_review) - seen))  # rows missing from the sheet altogether
    return SheetReport(decisions, pending, problems)


# ---- finalize: freeze the blind judgments ------------------------------------------------------

def finalize(report: SheetReport, sheet_path: Path, mapping_path: Path, frozen_path: Path,
             today: date) -> Path:
    """Freeze the owner's judgments. Requires a complete, valid sheet; never overwrites. The
    frozen record holds no proposed label, so it is safe to write before any comparison."""
    if report.problems or report.pending:
        raise ReviewError("the sheet is not complete and valid; run `bench review validate`")
    record = {
        "format": _FORMAT_VERSION,
        "kind": "owner-blind-review",
        "finalized_on": today.isoformat(),  # when this freeze ran; not a review date
        "sheet_sha256": _text_digest(sheet_path),
        "mapping_sha256": _text_digest(mapping_path),
        "decisions": [
            {"review_id": d.review_id, "case_id": d.case_id, "type": d.case_type.value,
             "label": d.label, "uncertain": d.uncertain, "note": d.note,
             "reviewer": d.reviewer, "reviewed_on": d.reviewed_on.isoformat()}
            for d in sorted(report.decisions, key=lambda d: d.review_id)
        ],
    }
    _create(frozen_path, json.dumps(record, indent=2) + "\n")
    return frozen_path


# ---- compare: only after the freeze ------------------------------------------------------------

@dataclass(frozen=True)
class Comparison:
    case_id: str
    case_type: CaseType
    critical: bool
    owner_label: str
    proposed_label: str
    owner_uncertain: bool

    @property
    def agrees(self) -> bool:
        return self.owner_label == self.proposed_label


def compare(frozen_path: Path, sheet_path: Path, mapping_path: Path,
            cases: Sequence[BenchmarkCase]) -> list[Comparison]:
    """Lay the owner's frozen judgments beside the proposed labels. Read-only: neither side is
    changed, and a difference is reported, never resolved. This is not a human-agreement figure:
    the proposed labels are not an independent human judgment."""
    if not frozen_path.exists():
        raise ReviewError(f"{frozen_path} not found: the blind review is not finalized, so "
                          "proposed labels are not compared (run `bench review finalize`)")
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    if frozen.get("mapping_sha256") != _text_digest(mapping_path):
        raise ReviewError("the id mapping changed after the review was finalized")
    if sheet_path.exists() and frozen.get("sheet_sha256") != _text_digest(sheet_path):
        raise ReviewError("the decision sheet was edited after the review was finalized; the "
                          "frozen record is the original judgment")
    verify_mapping(load_mapping(mapping_path), cases)
    by_id = {c.id: c for c in cases}
    return [Comparison(d["case_id"], CaseType(d["type"]), by_id[d["case_id"]].critical,
                       d["label"], by_id[d["case_id"]].expected.value, d["uncertain"])
            for d in frozen["decisions"]]


def render_comparison(rows: Sequence[Comparison]) -> str:
    differing = [r for r in rows if not r.agrees]
    critical = [r for r in differing if r.critical]
    lines = [
        "Owner blind judgments vs proposed labels (read-only; nothing was changed)",
        "The proposed labels are not an independent human judgment, so this is a discrepancy "
        "check, not an agreement statistic.",
        f"  cases compared: {len(rows)}",
        f"  same label:     {len(rows) - len(differing)}",
        f"  differing:      {len(differing)} ({len(critical)} critical)",
        f"  owner marked uncertain: {sum(r.owner_uncertain for r in rows)}",
    ]
    for r in differing:
        flag = " [critical]" if r.critical else ""
        lines.append(f"  DIFFERS {r.case_id}{flag}: owner {r.owner_label}, proposed "
                     f"{r.proposed_label}")
    return "\n".join(lines)


def comparison_json(rows: Sequence[Comparison]) -> str:
    return json.dumps([{"case_id": r.case_id, "type": r.case_type.value, "critical": r.critical,
                        "owner_label": r.owner_label, "proposed_label": r.proposed_label,
                        "owner_uncertain": r.owner_uncertain, "agrees": r.agrees}
                       for r in rows], indent=2) + "\n"
