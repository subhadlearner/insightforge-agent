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

from insightforge_agent.domain.contracts import UNVERIFIED_MARK
from insightforge_agent.domain.provenance import asserted_names, common_words, tokens

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


def b1_outcome_kind(outcome: str) -> str:
    """How a B-1 result was reached (diagnostic; `b1_failures` is unchanged):
    'supported'; 'judged' (a valid reply judged no span SUPPORTED); 'screened' (stopped by the
    deterministic gates, no call); 'unusable' (a call was made but its reply could not be read
    or judged nothing); 'not_sent' (over the per-call cap, no call). Unknown text is
    'unusable', never 'judged'."""
    if outcome == "SUPPORTED":
        return "supported"
    if outcome.startswith("not supported: no candidate span was judged SUPPORTED"):
        return "judged"
    if outcome.startswith("blocked before entailment") or outcome == "no candidate":
        return "screened"
    if outcome.startswith("not supported: the entailment request"):
        return "not_sent"
    return "unusable"


def b1_breakdown(record: dict) -> dict:
    """Per group, how many results were reached each way: a negative 'judged' is a valid
    semantic rejection; 'screened' and 'unusable' negatives passed the rule without one."""
    out = {}
    for group in ("positives", "negatives", "verified"):
        counts: dict[str, int] = {}
        for r in record.get(group, []):
            kind = b1_outcome_kind(r["outcome"])
            counts[kind] = counts.get(kind, 0) + 1
        out[group] = dict(sorted(counts.items()))
    return out


def b1_proposed_reliability_failures(record: dict) -> list[str]:
    """PROPOSED, NOT GATING (prospective only; it needs owner approval before it may decide a
    run, and never re-decides an earlier one): every call B-1 makes must return a reply the
    production parser can read. Lists each case whose result came from an unusable reply."""
    return [f"{group[:-1]} {r['ref']} {r.get('id', '')}: {r['outcome']}".replace("  ", " ")
            for group in ("positives", "negatives", "verified") for r in record.get(group, [])
            if b1_outcome_kind(r["outcome"]) == "unusable"]


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

# The stored Reports B-3 judges: T18's five, by provider and sample, each with its Report id.
T18_JUDGE_SAMPLES = {
    ("anthropic", "competitive-landscape-cloud"): "rep_3489459c430cef66",
    ("anthropic", "document-annual-report"): "rep_741a37d0ab3d8397",
    ("anthropic", "market-sizing-solar"): "rep_95e98f158ef7e398",
    ("anthropic", "memory-prior-report"): "rep_6e48a16b54b2cc51",
    ("anthropic", "regulatory-impact-semiconductors"): "rep_e0d086224779e53f",
}
JUDGE_CRITERIA = ("coverage", "organisation", "synthesis", "calibration")  # synthesis-rubric-v1


def _valid_scores(scores) -> bool:
    """Exactly the four rubric criteria, each a whole number from 1 to 5 (never a bool)."""
    return (isinstance(scores, dict) and set(scores) == set(JUDGE_CRITERIA)
            and all(type(v) is int and 1 <= v <= 5 for v in scores.values()))


def b3_outcome(judge_report: dict,
               expected: dict = T18_JUDGE_SAMPLES) -> tuple[str, list[str]]:
    """'pass' only when each expected stored Report has exactly one result, judged with valid
    scores. 'failed' when any result is refused or errored (its error type is detail, not
    success), has malformed scores, is a duplicate, names an unexpected sample or another Report.
    'inconclusive' when nothing failed but an expected Report has no result."""
    problems, seen = [], {}
    for r in judge_report.get("results", []):
        key = (r.get("provider"), r.get("sample_id"))
        synthesis = r.get("synthesis") or {}
        name = f"{key[0]}/{key[1]}"
        if key not in expected:
            problems.append(f"{name}: unexpected sample")
        elif key in seen:
            problems.append(f"{name}: more than one result")
        elif r.get("report_id") != expected[key]:
            problems.append(f"{name}: judged Report {r.get('report_id')}, expected "
                            f"{expected[key]}")
        elif synthesis.get("status") != "judged":
            problems.append(f"{name}: {synthesis.get('status')}: {synthesis.get('reason', '')}")
        elif not _valid_scores(synthesis.get("scores")):
            problems.append(f"{name}: malformed scores {synthesis.get('scores')!r}")
        seen.setdefault(key, r)
    if problems:
        return "failed", problems
    missing = [f"{p}/{s}: no result" for p, s in expected if (p, s) not in seen]
    if missing:
        return "inconclusive", missing
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
_COMPARED = re.compile(r"(?:compared (?:with|to)|versus|vs\.?|relative to|against|than|from|over)"
                       r"\s+(?:the\s+)?(?:fiscal\s+(?:year\s+)?|FY\s?)?$", re.IGNORECASE)
_NORTHWIND = re.compile(r"\bNorthwind\b", re.IGNORECASE)
_PERIOD_LABEL = re.compile(r"(?:H[12]|Q[1-4]|FY\d*)$", re.IGNORECASE)  # never a company name
# A forecast ("expects approval in H1 2026"), and the same event stated as already done.
_FORECAST = re.compile(r"\b(?:expect\w*|anticipat\w*|forecast\w*|project(?:s|ed)|plan(?:s|ned)?|"
                       r"target\w*|on track|aim\w*|due|scheduled|will|would|may|might|should|"
                       r"likely|pending|awaiting)\b", re.IGNORECASE)
_DONE = re.compile(r"\b(?:received|obtained|secured|won|gained|achieved|"
                   r"(?:was|were|has been|have been|had been) (?:\w+ )?(?:approved|granted|cleared)|"
                   r"(?<!be )granted)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Fact:
    """A fact the sample Northwind PDF states, as matchable parts of one sentence."""

    terms: tuple[str, ...]  # each must appear in the sentence
    figure: str | None = None  # the exact figure stated, if any
    years: frozenset[str] | None = None  # the measured period; another year is a mismatch
    segment: str | None = None  # whose figure: a segment, or "total" (no segment named)
    compared_with: frozenset[str] = frozenset()  # a comparison base, allowed only as one
    expected: bool = False  # a forecast: stated as expected, never as already done


NORTHWIND_FACTS = {
    "revenue 612 million": Fact((r"revenue",), r"\b612 million\b", frozenset({"2025"}), "total"),
    "growth 14 percent": Fact((r"\bgr[eo]w|growth|increas|\brose\b",), r"\b14 ?(?:percent|%)",
                              frozenset({"2025"}), "total", frozenset({"2024"})),
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
                                             frozenset({"2026"}), expected=True),
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
    if fact.expected:
        done, forecast = _DONE.search(sentence), _FORECAST.search(sentence)
        if done and not forecast:
            return "mismatch"  # stated as already done ("NW-200 received approval")
        if done or not forecast:
            return "candidate"  # both, or neither: not settled
    if fact.years is not None:
        stated = [(m.group(), m.start()) for m in _YEAR.finditer(sentence)]
        # A comparison base ("compared with fiscal 2024") is not the measured period.
        bases = {y for y, at in stated if _COMPARED.search(sentence[:at])}
        measured = {y for y, _ in stated} - (bases & fact.compared_with)
        if fact.compared_with and bases - fact.compared_with:
            return "mismatch"  # another comparison base ("compared with fiscal 2023")
        if measured and not measured & fact.years:
            return "mismatch"  # another period ("grew 14 percent in fiscal 2024")
        if not stated:
            return "candidate"  # the period is not stated: not established
    return "match"


def judge_fact(fact: Fact, text: str) -> str:
    """'match', 'candidate', 'mismatch' or 'absent': the best verdict over the text's sentences."""
    verdicts = [_judge_sentence(fact, s) for s in _SENTENCE.split(text.strip()) if s]
    return max(verdicts or ["absent"], key=_RANK.__getitem__)


def _claims_and_items(body: dict) -> tuple[list[dict], dict[str, dict]]:
    """The published Report's current Claims (`verified.draft`, the Report a reader sees; never
    the pre-check draft, never Historical Claims), each with `unverified` set when the
    Fact-Checker judged it so (its verdict, or the mark on its text), and its text without the
    mark; and the retained Evidence items by id. An unsampled Claim has no verdict, and none is
    invented: it counts as published, neither verified nor unverified."""
    verified = body["verified"]
    unverified = {(v["section"], v["claim"]) for v in verified.get("verdicts", [])
                  if v["verdict"] == "unverified"}
    claims = []
    for si, section in enumerate(verified["draft"]["sections"]):
        for ci, c in enumerate(section["claims"]):
            if c.get("historical"):
                continue
            marked = c["text"].endswith(UNVERIFIED_MARK)
            claims.append({**c, "text": c["text"].removesuffix(UNVERIFIED_MARK),
                           "unverified": marked or (si, ci) in unverified})
    items = {i["id"]: i for s in body["evidence"]["sections"] for i in s["items"]}
    return claims, items


def _attribution(item: dict | None) -> str:
    """Whose fact a retained Evidence item states: 'northwind' when it or its entity names
    Northwind, 'other' when its entity is someone else, 'unknown' otherwise."""
    if item is None:
        return "unknown"
    entity = item.get("entity") or ""
    if _NORTHWIND.search(item["statement"]) or _NORTHWIND.search(entity):
        return "northwind"
    return "other" if entity.strip() else "unknown"


def _attributed(verdict: str, claim: str, item: dict | None, common: frozenset[str]) -> str:
    """A Claim's verdict once its company is checked. A Claim naming Northwind is Northwind's.
    One that names a company its cited Evidence does not ("Medtronic revenue reached 612
    million"), or cites Evidence about another company, is a mismatch. One that names no company
    is attributed through its cited Evidence; without that, it is only a candidate."""
    if verdict not in ("match", "candidate") or _NORTHWIND.search(claim):
        return verdict
    cited = item or {"statement": "", "entity": ""}
    known = {t.casefold() for t in tokens(f"{cited['statement']} {cited.get('entity') or ''}")}
    if any(n.casefold() not in known and not _PERIOD_LABEL.match(n)
           for n in asserted_names(claim, common)):
        return "mismatch"  # attributed to a company the cited Evidence does not name
    who = _attribution(item)
    if who == "other":
        return "mismatch"
    return verdict if who == "northwind" else "candidate"


def document_coverage(body: dict) -> dict:
    """Per Northwind fact, over the published Report, in this order: 'mismatch' if any Claim
    misstates it (another segment's figure, another period, another company, negated), even
    beside a correct one or when marked unverified; 'covered' if a Claim the Fact-Checker did
    not mark unverified states it and cites retained Evidence that states it; 'unverified claim'
    if only unverified Claims do (manual adjudication); 'candidate' if only one side states it
    or a side is unsettled (manual adjudication); 'retained, not claimed'; 'mismatch' if only
    Evidence misstates it; else 'missing'. Gap lines are never read."""
    claims, items = _claims_and_items(body)
    common = common_words([c["text"] for c in claims] + [i["statement"] for i in items.values()])
    facts = {}
    for name, fact in NORTHWIND_FACTS.items():
        on_items = {i: _attributed(judge_fact(fact, x["statement"]), x["statement"], x, common)
                    for i, x in items.items()}
        pairs = [(_attributed(judge_fact(fact, c["text"]), c["text"],
                              items.get(c["evidence_id"]), common),
                  on_items.get(c["evidence_id"], "absent"), c["unverified"]) for c in claims]
        if any(claim == "mismatch" for claim, _, _ in pairs):
            facts[name] = "mismatch"
        elif ("match", "match", False) in pairs:
            facts[name] = "covered"
        elif ("match", "match", True) in pairs:
            facts[name] = "unverified claim"
        elif any(claim in ("match", "candidate") or item in ("match", "candidate")
                 for claim, item, _ in pairs) or "candidate" in on_items.values():
            facts[name] = "candidate"
        elif "match" in on_items.values():
            facts[name] = "retained, not claimed"
        elif "mismatch" in on_items.values():
            facts[name] = "mismatch"
        else:
            facts[name] = "missing"
    values = set(facts.values())
    status = ("failed" if values & {"missing", "mismatch", "retained, not claimed"} else
              "inconclusive" if values & {"candidate", "unverified claim"} else "pass")
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
    """Per objective, over the published Report: 'covered' (a Claim not marked unverified states
    a known fact and cites retained Evidence that does), 'unverified claim' (only unverified
    Claims do: manual adjudication), 'candidate' (another figure-bearing statement on it: manual
    review), 'retained, not claimed', or, when absent, 'pipeline loss' if the Run's own fetched
    text stated it (a failure) or 'not fetched' (external unavailability: inconclusive)."""
    claims, items = _claims_and_items(body)
    out = {}
    for name, o in REGULATORY_OBJECTIVES.items():
        def about(text: str) -> bool:
            return bool(re.search(o["about"], text, re.IGNORECASE)
                        and not re.search(o["not"], text, re.IGNORECASE)
                        and _FIGURE.search(text) and not _NEGATION.search(text))

        def known(text: str) -> bool:
            return about(text) and any(re.search(k, text, re.IGNORECASE) for k in o["known"])

        pairs = [(c["text"], items[c["evidence_id"]]["statement"] if c["evidence_id"] in items
                  else "", c["unverified"]) for c in claims]
        if any(known(c) and known(i) and not u for c, i, u in pairs):
            out[name] = "covered"
        elif any(known(c) and known(i) for c, i, _ in pairs):
            out[name] = "unverified claim"
        elif any(about(c) and about(i) for c, i, _ in pairs):
            out[name] = "candidate"
        elif any(about(i["statement"]) for i in items.values()):
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
