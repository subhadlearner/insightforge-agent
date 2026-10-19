"""T18 live-validation acceptance rules (Stages B and C), as pure functions over recorded
results, so each rule is unit-tested offline before any paid run (tests/test_t18_acceptance.py)
and applied unchanged by the live harness (tests/live/test_t18_stage_b.py) or to a finished
evaluation:

    uv run python -m tests.t18_acceptance b3 <judge-report.json>
    uv run python -m tests.t18_acceptance stage-c <evaluation dir>

Each rule returns failures (or a status) with case-level detail. A rule never passes on a
diagnostic alone: an error type, a logged reason or a Gap line explains a miss, it is not a
success. External unavailability (the Run never fetched the facts) is kept apart from pipeline
loss (the Run fetched them and lost them): the first is inconclusive, the second a failure."""

import json
import re
import sqlite3
import sys
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

# The facts the sample Northwind PDF states (samples/briefs/document-annual-report.yml and its
# document): each must be retained Evidence and cited by a Report Claim.
NORTHWIND_FACTS = {
    "revenue 612 million": r"\b612 million\b",
    "growth 14 percent": r"\b14 ?(?:percent|%)",
    "cardiology segment 341 million": r"\b341 million\b",
    "orthopaedic segment 187 million": r"\b187 million\b",
    "diagnostics segment 84 million": r"\b84 million\b",
    "R&D spending 71 million": r"\b71 million\b",
    "6 programmes in clinical trials": r"\b(?:6|six) (?:clinical )?programmes?\b",
    "NW-200 pivotal trial": r"NW-200.*pivotal|pivotal.*NW-200",
    "NW-200 approval expected H1 2026": r"(?=.*NW-200)(?=.*approv)(?=.*2026)",  # any order
    "single titanium supplier risk": r"titanium",
    "hospital pricing pressure risk": r"pricing pressure|average selling prices?",
    "NW-200 delay risk": r"delays? to the NW-200|NW-200.*delay|delay.*NW-200",
    "fiscal 2026 outlook 9 to 11 percent": r"\b9 (?:to|-) ?11 ?(?:percent|%)",
}

# The regulatory Brief's core objective (Nvidia's China revenue and the H20 effect). Known facts
# from the sources T18 fetched pass outright; any other China-revenue or H20-charge statement is a
# candidate for manual review (a revenue-sharing arrangement is not China revenue).
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


def _retained_and_claimed(body: dict) -> list[tuple[str, bool]]:
    """Each retained Evidence statement, and whether a current Report Claim cites it."""
    cited = {c["evidence_id"] for s in body["draft"]["sections"] for c in s["claims"]
             if not c.get("historical")}
    return [(i["statement"], i["id"] in cited)
            for s in body["evidence"]["sections"] for i in s["items"]]


def document_coverage(body: dict) -> dict:
    """Per Northwind fact: 'covered' (retained Evidence cited by a Claim), 'retained, not
    claimed', or 'missing'. Gap lines are never read: they explain, they do not cover."""
    items = _retained_and_claimed(body)
    out = {}
    for fact, pattern in NORTHWIND_FACTS.items():
        hits = [claimed for statement, claimed in items
                if re.search(pattern, statement, re.IGNORECASE)]
        out[fact] = "covered" if any(hits) else ("retained, not claimed" if hits else "missing")
    status = "pass" if all(v == "covered" for v in out.values()) else "failed"
    return {"status": status, "facts": out}


def regulatory_coverage(body: dict, fetched_texts: list[str]) -> dict:
    """Per objective: 'covered' (a known fact retained and claimed), 'candidate' (another
    figure-bearing statement on the objective, retained and claimed: manual review), or, when
    absent, 'pipeline loss' if the Run's own fetched text stated it (a failure) or 'not fetched'
    (external unavailability: inconclusive)."""
    items = [(s, c) for s, c in _retained_and_claimed(body) if c]
    out = {}
    for name, o in REGULATORY_OBJECTIVES.items():
        def about(text: str) -> bool:
            return bool(re.search(o["about"], text, re.IGNORECASE)
                        and not re.search(o["not"], text, re.IGNORECASE)
                        and _FIGURE.search(text))
        if any(any(re.search(k, s, re.IGNORECASE) for k in o["known"]) and about(s)
               for s, _ in items):
            out[name] = "covered"
        elif any(about(s) for s, _ in items):
            out[name] = "candidate"
        elif any(about(t) for t in fetched_texts):
            out[name] = "pipeline loss"
        else:
            out[name] = "not fetched"
    values = set(out.values())
    status = ("failed" if "pipeline loss" in values else
              "pass" if values == {"covered"} else
              "inconclusive")
    return {"status": status, "objectives": out}


def stage_c(evaluation_dir: Path) -> dict:
    """Applies the Stage C coverage rules to a finished evaluation directory (read-only)."""
    report = json.loads((evaluation_dir / "eval-report.json").read_text(encoding="utf-8"))
    out = {}
    for provider in report["providers"]:
        db = evaluation_dir / provider["provider"] / "eval.db"
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            for s in provider["samples"]:
                if s["sample_id"] not in ("document-annual-report",
                                          "regulatory-impact-semiconductors"):
                    continue
                if s["status"] != "complete":
                    out[s["sample_id"]] = {"status": "failed", "reason": s["status"]}
                    continue
                body = json.loads(con.execute("SELECT body FROM reports WHERE owner_id=? AND "
                                              "id=?", (s["owner_id"], s["report_id"])).fetchone()[0])
                if s["sample_id"] == "document-annual-report":
                    out[s["sample_id"]] = document_coverage(body)
                else:
                    out[s["sample_id"]] = regulatory_coverage(body, fetched_texts(con, s["run_id"]))
        finally:
            con.close()
    return out


def fetched_texts(con: sqlite3.Connection, run_id: str) -> list[str]:
    """The text the Run itself fetched, without the Fact-Checker's re-fetches."""
    refetched = {json.loads(p)["observation_id"] for (p,) in con.execute(
        "SELECT payload FROM run_events WHERE run_id=? AND type='fact_check_refetched'", (run_id,))}
    return [t for o, t in con.execute(
        "SELECT p.observation_id, p.text FROM passages p JOIN observations ob "
        "ON ob.id = p.observation_id WHERE ob.run_id=?", (run_id,)) if o not in refetched]


if __name__ == "__main__":
    command, target = sys.argv[1], Path(sys.argv[2])
    if command == "b3":
        status, detail = b3_outcome(json.loads(target.read_text(encoding="utf-8")))
        print(json.dumps({"status": status, "detail": detail}, indent=1))
    elif command == "stage-c":
        print(json.dumps(stage_c(target), indent=1))
    else:
        raise SystemExit(f"unknown command {command!r}")
