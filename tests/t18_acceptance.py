"""T18 live-validation acceptance rules (Stages B and C), as pure functions over recorded
results, so each rule is unit-tested offline before any paid run (tests/test_t18_acceptance.py)
and applied unchanged by the live harness (tests/live/test_t18_stage_b.py) or to a finished
evaluation:

    uv run python -m tests.t18_acceptance b3 <judge-report.json>
    uv run python -m tests.t18_acceptance stage-c <evaluation dir>

Both commands print a JSON result and exit 0 only on pass: 1 failed, 2 inconclusive, 3 the input
could not be read. They read their input and never write it.

Each rule returns failures (or a status) with case-level detail. A rule never passes on a
diagnostic alone: an error type, a logged reason or a Gap line explains a miss, it is not a
success. External unavailability (the Run never fetched the facts) is kept apart from pipeline
loss (the Run fetched them and lost them): the first is inconclusive, the second a failure."""

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- Stage B-1: entailment calibration -------------------------------------------------------

CLEARLY_SUPPORTED = "yes"  # the adjudication recorded before any paid call
EXPECTED_VERIFIED = 27


def b1_failures(record: dict, cases: dict) -> list[str]:
    """Every negative judged not SUPPORTED; each of the 8 positives adjudicated "yes" judged
    SUPPORTED (Germany "partly" and the doubtful H20 Claim are reported only); all 27 of T18's
    verified Claims still SUPPORTED; and no case missing."""
    failures = []
    by_case = {(r["ref"], r["id"]): r for r in record.get("negatives", [])}
    for g in cases["groups"]:
        for n in g["negatives"]:
            row = by_case.get((g["ref"], n["id"]))
            if row is None:
                failures.append(f"negative {n['id']} ({g['ref']}): no result recorded")
            elif row["outcome"] == "SUPPORTED":
                failures.append(f"negative {n['id']} ({n['kind']}, {g['ref']}) was judged "
                                f"SUPPORTED: {n['text']!r}")
    positives = {r["ref"]: r for r in record.get("positives", [])}
    for g in cases["groups"]:
        if cases["adjudication"][g["ref"]] != CLEARLY_SUPPORTED:
            continue
        row = positives.get(g["ref"])
        if row is None:
            failures.append(f"positive {g['ref']}: no result recorded")
        elif row["outcome"] != "SUPPORTED":
            failures.append(f"positive {g['ref']} (adjudicated clearly supported) was not "
                            f"SUPPORTED: {row['outcome']}; Claim {g['positive']!r}")
    verified = record.get("verified", [])
    if len(verified) != EXPECTED_VERIFIED:
        failures.append(f"{len(verified)} of the {EXPECTED_VERIFIED} T18-verified Claims recorded")
    failures += [f"T18-verified Claim {r['ref']} lost: {r['outcome']}" for r in verified
                 if r["outcome"] != "SUPPORTED"]
    return failures


# ---- Stage B-2: drafting smoke test ----------------------------------------------------------

CAPS = ("None", "150", "120")


def b2_classify(row: dict) -> str:
    """'ok' (parsed), 'truncated' (the provider stopped at max_tokens, whatever the text), or
    'failure': anything else that is not the schema, incomplete JSON without a max_tokens stop
    included, and a provider rejection of the schema."""
    if "items" in row:
        return "ok"
    if row.get("stop_reason") == "max_tokens":
        return "truncated"
    return "failure"


def b2_failures(record: dict) -> list[str]:
    """The syntax rule, applied to every cap configuration independently."""
    failures = []
    for cap in CAPS:
        rows = record.get(cap)
        if not rows:
            failures.append(f"cap {cap}: no calls recorded")
            continue
        failures += [f"cap {cap}, call {n}: {row}" for n, row in enumerate(rows, start=1)
                     if b2_classify(row) == "failure"]
    return failures


def b2_summary(record: dict) -> dict:
    """Per cap: calls, parsed, truncated, failures and items, for the cap decision."""
    out = {}
    for cap in CAPS:
        rows = record.get(cap) or []
        kinds = [b2_classify(r) for r in rows]
        out[cap] = {"calls": len(rows), "parsed": kinds.count("ok"),
                    "truncated": kinds.count("truncated"), "failures": kinds.count("failure"),
                    "items": sum(r.get("items", 0) for r in rows)}
    return out


def b2_cap_decision(record: dict) -> str:
    """Spec v3 CS3b: keep no cap if it has zero truncations; else the smallest cap (150, then
    120) with zero truncations and at least as many items as no cap; else undecided."""
    s = b2_summary(record)
    if s["None"]["truncated"] == 0:
        return "None"
    for cap in ("150", "120"):
        if s[cap]["truncated"] == 0 and s[cap]["items"] >= s["None"]["items"]:
            return cap
    return "undecided"


# ---- Stage B-3: judge-only -------------------------------------------------------------------

def b3_outcome(judge_report: dict, expected_samples: int = 5) -> tuple[str, list[str]]:
    """'pass' only when every one of the expected stored Reports was judged with valid scores;
    'failed' when any was refused or errored (its error type is detail, not success);
    'inconclusive' when fewer Reports were judged than expected."""
    results = judge_report.get("results", [])
    judged = [r for r in results if r["synthesis"]["status"] == "judged"
              and r["synthesis"].get("scores")]
    problems = [f"{r['sample_id']}: {r['synthesis']['status']}: {r['synthesis'].get('reason', '')}"
                for r in results if r not in judged]
    if problems:
        return "failed", problems
    if len(judged) != expected_samples:
        return "inconclusive", [f"{len(judged)} of {expected_samples} Reports judged"]
    return "pass", []


# ---- Stage C: research coverage --------------------------------------------------------------
#
# Deterministic matching can show a fact is stated, or plainly misstated (another segment's
# figure, a negated or other-period statement); it cannot prove meaning. So a fact is "covered"
# only when a Report Claim's own text states it and cites retained Evidence that states it too;
# anything the rules cannot settle is a "candidate" for manual adjudication, never a pass.

_SENTENCE = re.compile(r"(?<=[.!?;])\s+")
_YEAR = re.compile(r"\b20\d\d\b")
_NEGATION = re.compile(r"\b(?:not|no|never|without|denied|rejected|unlikely|cannot|can't|won't|"
                       r"didn't|doesn't|isn't|wasn't|failed|fails)\b", re.IGNORECASE)
_SEGMENTS = {"cardiology": r"cardiolog\w*", "orthopaedic": r"orthop(?:a)?edic\w*",
             "diagnostics": r"diagnostic\w*"}


@dataclass(frozen=True)
class Fact:
    """A fact the sample Northwind PDF states, as matchable parts of one sentence."""

    terms: tuple[str, ...]  # each must appear in the sentence
    figure: str | None = None  # the exact figure stated, if any
    years: frozenset[str] | None = None  # allowed years; another year is a mismatch
    segment: str | None = None  # whose figure: a segment, or "total" (no segment named)


NORTHWIND_FACTS = {
    "revenue 612 million": Fact((r"revenue",), r"\b612 million\b", frozenset({"2025"}), "total"),
    "growth 14 percent": Fact((r"\bgr[eo]w|growth|increas|\brose\b",), r"\b14 ?(?:percent|%)",
                              frozenset({"2024", "2025"}), "total"),
    "cardiology segment 341 million": Fact((r"contribut|revenue|sales",), r"\b341 million\b",
                                           frozenset({"2025"}), "cardiology"),
    "orthopaedic segment 187 million": Fact((r"contribut|revenue|sales",), r"\b187 million\b",
                                            frozenset({"2025"}), "orthopaedic"),
    "diagnostics segment 84 million": Fact((r"contribut|revenue|sales",), r"\b84 million\b",
                                           frozenset({"2025"}), "diagnostics"),
    "R&D spending 71 million": Fact((r"research and development|R&D",
                                     r"spen[dt]|expens|expenditure|invest"), r"\b71 million\b",
                                    frozenset({"2025"})),
    "6 programmes in clinical trials": Fact((r"programmes?|programs?", r"clinical|trial"),
                                            r"\b(?:6|six)\b", frozenset({"2025"})),
    "NW-200 pivotal trial": Fact((r"NW-200", r"pivotal", r"complet"), None, frozenset({"2025"})),
    "NW-200 approval expected H1 2026": Fact((r"NW-200", r"approv", r"first half|\bH1\b"), None,
                                             frozenset({"2026"})),
    "single titanium supplier risk": Fact((r"\b(?:single|sole|one)\b", r"supplier", r"titanium")),
    "hospital pricing pressure risk": Fact((r"pric", r"hospital|purchasing",
                                            r"pressure|reduc")),
    "NW-200 delay risk": Fact((r"delay", r"NW-200", r"defer|cardiology revenue")),
    "fiscal 2026 outlook 9 to 11 percent": Fact((r"expect|outlook|guidance|forecast",),
                                                r"\b9 ?(?:to|-) ?11 ?(?:percent|%)",
                                                frozenset({"2026"}), "total"),
}
_RANK = {"match": 3, "candidate": 2, "mismatch": 1, "absent": 0}


def _segment_of(sentence: str, at: int) -> str | None:
    """The segment named nearest before position `at` (else nearest after), if any."""
    found = [(m.start(), name) for name, pattern in _SEGMENTS.items()
             for m in re.finditer(pattern, sentence, re.IGNORECASE)]
    if not found:
        return None
    before = [f for f in found if f[0] <= at]
    return (max(before) if before else min(found))[1]


def _judge_sentence(fact: Fact, sentence: str) -> str:
    # A figure is a whole number, never part of a decimal or a longer number ("6" in "6.34").
    figure = (re.search(rf"(?<![\d.,]){fact.figure}(?![.,]?\d)", sentence, re.IGNORECASE)
              if fact.figure else None)
    if fact.figure and not figure:
        return "absent"
    if not all(re.search(t, sentence, re.IGNORECASE) for t in fact.terms):
        return "absent"  # not about this fact at all
    if fact.segment and figure:
        named = _segment_of(sentence, figure.start())
        if (fact.segment == "total" and named) or (fact.segment != "total" and named != fact.segment):
            return "mismatch"  # another segment's figure, or a segment's figure as the total
    if _NEGATION.search(sentence):
        return "mismatch"
    if fact.years is not None:
        years = set(_YEAR.findall(sentence))
        if years and not years & fact.years:
            return "mismatch"  # another period
        if not years:
            return "candidate"  # the period is not stated: not established
    return "match"


def judge_fact(fact: Fact, text: str) -> str:
    """'match', 'candidate', 'mismatch' or 'absent': the best verdict over the text's sentences."""
    verdicts = [_judge_sentence(fact, s) for s in _SENTENCE.split(text.strip()) if s]
    return max(verdicts or ["absent"], key=_RANK.__getitem__)


def _claims_and_items(body: dict) -> tuple[list[dict], dict[str, str]]:
    """Current Report Claims, and retained Evidence statements by id."""
    claims = [c for s in body["draft"]["sections"] for c in s["claims"] if not c.get("historical")]
    items = {i["id"]: i["statement"] for s in body["evidence"]["sections"] for i in s["items"]}
    return claims, items


def document_coverage(body: dict) -> dict:
    """Per Northwind fact, in this order: 'mismatch' if any Claim misstates it (another
    segment's figure, another period, negated), even beside a correct one; 'covered' if a Claim
    states it and cites retained Evidence that states it; 'candidate' if only one side states it
    or a side is unsettled (manual adjudication); 'retained, not claimed'; 'mismatch' if only
    Evidence misstates it; else 'missing'. Gap lines are never read."""
    claims, items = _claims_and_items(body)
    facts = {}
    for name, fact in NORTHWIND_FACTS.items():
        on_items = {i: judge_fact(fact, s) for i, s in items.items()}
        pairs = [(judge_fact(fact, c["text"]), on_items.get(c["evidence_id"], "absent"))
                 for c in claims]
        if any(claim == "mismatch" for claim, _ in pairs):
            facts[name] = "mismatch"
        elif ("match", "match") in pairs:
            facts[name] = "covered"
        elif any(claim in ("match", "candidate") or item in ("match", "candidate")
                 for claim, item in pairs) or "candidate" in on_items.values():
            facts[name] = "candidate"
        elif "match" in on_items.values():
            facts[name] = "retained, not claimed"
        elif "mismatch" in on_items.values():
            facts[name] = "mismatch"
        else:
            facts[name] = "missing"
    values = set(facts.values())
    status = ("failed" if values & {"missing", "mismatch", "retained, not claimed"} else
              "inconclusive" if "candidate" in values else "pass")
    return {"status": status, "facts": facts}


# The regulatory Brief's core objective (Nvidia's China revenue and the H20 effect). Known facts
# from the sources T18 fetched pass outright; any other figure-bearing statement on the objective
# is a candidate for manual review (a revenue-sharing arrangement is not China revenue).
REGULATORY_OBJECTIVES = {
    "Nvidia China revenue": {
        "known": [r"17(?:\.1)? billion", r"\b13 ?%.*(?:revenue|sales)|(?:revenue|sales).*\b13 ?%",
                  r"no China data center|zero China"],
        "about": r"(?=.*\bChina\b)(?=.*\b(?:revenue|sales)\b)(?=.*(?:Nvidia|NVIDIA))",
        "not": r"share of revenues|revenue[- ]shar|remit|% of revenues from",
    },
    "H20 charge or lost sales": {
        "known": [r"\b4\.5 billion|\b5\.5 billion|\b4,538 million|\b2\.5 billion"],
        "about": r"(?=.*\bH20\b)(?=.*(?:charge|write-?down|inventory|lost|loss))",
        "not": r"$^",
    },
}
_FIGURE = re.compile(r"\$?\d[\d,.]*\s*(?:billion|million|%|percent)", re.IGNORECASE)


def regulatory_coverage(body: dict, fetched_texts: list[str]) -> dict:
    """Per objective: 'covered' (a Claim states a known fact and cites retained Evidence that
    does), 'candidate' (another figure-bearing statement on it: manual review), 'retained, not
    claimed', or, when absent, 'pipeline loss' if the Run's own fetched text stated it (a
    failure) or 'not fetched' (external unavailability: inconclusive)."""
    claims, items = _claims_and_items(body)
    out = {}
    for name, o in REGULATORY_OBJECTIVES.items():
        def about(text: str) -> bool:
            return bool(re.search(o["about"], text, re.IGNORECASE)
                        and not re.search(o["not"], text, re.IGNORECASE)
                        and _FIGURE.search(text) and not _NEGATION.search(text))

        def known(text: str) -> bool:
            return about(text) and any(re.search(k, text, re.IGNORECASE) for k in o["known"])

        pairs = [(c["text"], items.get(c["evidence_id"], "")) for c in claims]
        if any(known(c) and known(i) for c, i in pairs):
            out[name] = "covered"
        elif any(about(c) and about(i) for c, i in pairs):
            out[name] = "candidate"
        elif any(about(i) for i in items.values()):
            out[name] = "retained, not claimed"
        elif any(about(t) for t in fetched_texts):
            out[name] = "pipeline loss"
        else:
            out[name] = "not fetched"
    values = set(out.values())
    status = ("failed" if values & {"pipeline loss", "retained, not claimed"} else
              "pass" if values == {"covered"} else "inconclusive")
    return {"status": status, "objectives": out}


STAGE_C_BRIEFS = ("document-annual-report", "regulatory-impact-semiconductors")
_WORST = {"pass": 0, "inconclusive": 1, "failed": 2}


def stage_c(evaluation_dir: Path) -> dict:
    """The Stage C coverage rules over a finished evaluation directory, read-only. Each target
    Brief must appear exactly once, as a complete sample; the overall status is the worst."""
    report = json.loads((evaluation_dir / "eval-report.json").read_text(encoding="utf-8"))
    found = {b: [(p["provider"], s) for p in report["providers"] for s in p["samples"]
                 if s["sample_id"] == b] for b in STAGE_C_BRIEFS}
    briefs, reasons = {}, []
    for brief, entries in found.items():
        if len(entries) != 1:
            briefs[brief] = {"status": "failed",
                             "reason": "missing" if not entries else f"{len(entries)} results"}
            reasons.append(f"{brief}: {briefs[brief]['reason']}")
            continue
        provider, s = entries[0]
        if s["status"] != "complete" or not s.get("report_id"):
            briefs[brief] = {"status": "failed", "reason": f"sample status {s['status']}"}
            reasons.append(f"{brief}: sample status {s['status']}")
            continue
        db = evaluation_dir / provider / "eval.db"
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT body FROM reports WHERE owner_id=? AND id=?",
                              (s["owner_id"], s["report_id"])).fetchone()
            if row is None:
                briefs[brief] = {"status": "failed", "reason": "stored Report not found"}
            elif brief == "document-annual-report":
                briefs[brief] = document_coverage(json.loads(row[0]))
            else:
                briefs[brief] = regulatory_coverage(json.loads(row[0]),
                                                    fetched_texts(con, s["run_id"]))
        finally:
            con.close()
        if briefs[brief]["status"] != "pass":
            detail = briefs[brief].get("facts") or briefs[brief].get("objectives") or {}
            reasons += [f"{brief}: {k}: {v}" for k, v in detail.items() if v != "covered"]
            if "reason" in briefs[brief]:
                reasons.append(f"{brief}: {briefs[brief]['reason']}")
    status = max((b["status"] for b in briefs.values()), key=_WORST.__getitem__)
    return {"status": status, "briefs": briefs, "reasons": reasons}


def fetched_texts(con: sqlite3.Connection, run_id: str) -> list[str]:
    """The text the Run itself fetched, without the Fact-Checker's re-fetches."""
    refetched = {json.loads(p)["observation_id"] for (p,) in con.execute(
        "SELECT payload FROM run_events WHERE run_id=? AND type='fact_check_refetched'", (run_id,))}
    return [t for o, t in con.execute(
        "SELECT p.observation_id, p.text FROM passages p JOIN observations ob "
        "ON ob.id = p.observation_id WHERE ob.run_id=?", (run_id,)) if o not in refetched]


EXIT = {"pass": 0, "failed": 1, "inconclusive": 2}  # 3: the input could not be read


def main(argv: list[str]) -> int:
    """`b3 <judge-report.json>` or `stage-c <evaluation dir>`: prints a JSON result and exits
    0 only on pass (1 failed, 2 inconclusive, 3 unreadable input or usage). Reads only."""
    if len(argv) != 2 or argv[0] not in ("b3", "stage-c"):
        print(json.dumps({"status": "error", "reasons": ["usage: b3 <judge-report.json> | "
                                                         "stage-c <evaluation dir>"]}))
        return 3
    command, target = argv[0], Path(argv[1])
    try:
        if command == "b3":
            status, detail = b3_outcome(json.loads(target.read_text(encoding="utf-8")))
            result = {"status": status, "reasons": detail}
        else:
            result = stage_c(target)
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        print(json.dumps({"status": "error", "reasons": [f"{type(e).__name__}: {e}"]}))
        return 3
    print(json.dumps(result, indent=1))
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
