"""The ADR-0006 live support gate: what it asks, and how its result is decided. No model is
called here; tests/live/test_support_gate_live.py runs it (paid, owner-authorised only),
tests/test_support_gate_decision.py checks the decision offline, and
tests/integration/test_historical_support_gate_plan.py builds every request from the saved T18
database without a call.

Cases, each judged by the production `judge_units` on the cited unit only:
- `gold`: tests/fixtures/research_integrity/support_cases.json (text in the fixture; uploaded-
  document cases with their identity context). Negatives whose floor is "model" are repeated.
- `b1_positive`, `b1_negative`: the 10 positives and 40 adversarial negatives of T18 B-1
  (stage_b1_cases.json), on the cited Passage's counterpart in the verifier's re-fetch, as B-1.
- `b1_verified`: the 27 T18-verified Claims, expected per the versioned gold
  (b1_gold_v2.json: 24 SUPPORTED, 3 INSUFFICIENT_EVIDENCE), on the same counterpart.
- `holdout`: tests/fixtures/research_integrity/support_holdout.json, written after the first
  live attempt and before the second, with no variant of a case that attempt judged; negatives
  are repeated.

Decision, fixed before any paid call: FAIL if any negative (or a Claim adjudicated
INSUFFICIENT_EVIDENCE) is SUPPORTED in any repetition, or any positive adjudicated clearly
supported is judged otherwise; INCONCLUSIVE if any gated case got VERIFICATION_ERROR or no row;
else PASS. Positives adjudicated only partly supported or doubtful are reported, not gated. One
pass is a regression gate on these cases, not proof of reliability in general."""

import json
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GATED_POSITIVE = "yes"  # stage_b1_cases.json adjudication of a clearly supported positive


@dataclass(frozen=True)
class Case:
    set: str
    id: str
    statement: str
    passage: str
    expected: str  # "SUPPORTED" or "NOT_SUPPORTED"; None-gated cases use "REPORT"
    entity: str | None = None
    repeats: int = 1
    ref: tuple[str, int] = ("gold", 0)
    meta: dict = field(default_factory=dict)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gold_cases(repeats: int) -> list[Case]:
    out = []
    for c in load("research_integrity/support_cases.json")["cases"]:
        positive = c["expected"] == "SUPPORTED"
        # Every negative is repeated: a floor that code holds only by a coincidental operator
        # elsewhere in the Passage (support-gold-2, Q-N02) must not lighten the live check.
        out.append(Case("gold", c["id"], c["statement"], c["passage"],
                        "SUPPORTED" if positive else "NOT_SUPPORTED", c["document_entity"],
                        repeats if not positive else 1,
                        meta={"kind": c["kind"], "class": c["class"]}))
    return out


def holdout_cases(repeats: int) -> list[Case]:
    return [Case("holdout", c["id"], c["statement"], c["passage"],
                 "SUPPORTED" if c["expected"] == "SUPPORTED" else "NOT_SUPPORTED",
                 c["document_entity"], 1 if c["expected"] == "SUPPORTED" else repeats,
                 meta={"kind": c["kind"]})
            for c in load("research_integrity/support_holdout.json")["cases"]]


def b1_cases(con, repeats: int) -> list[Case]:
    """The B-1 groups and the 27 T18-verified Claims, each on the counterpart of its cited
    Passage in the verifier's recorded re-fetch, read from the saved T18 database."""
    from insightforge_agent.pipeline.fact_check import counterpart
    from tests.t18_stage_b_support import passages, verdicts

    stored = verdicts(con)

    def unit(ref: str):
        v, claim = stored[ref]
        cited = next(p for p in passages(con, v["original"]["observation_id"])
                     if p.index == v["original"]["index"])
        found, match = counterpart(cited, passages(con, v["matched"]["observation_id"]))
        if found is None:
            raise LookupError(f"{ref}: no counterpart ({match})")
        return found, claim

    cases = load("t18/stage_b1_cases.json")
    out = []
    for g in cases["groups"]:
        found, _ = unit(g["ref"])
        where = (found.observation_id, found.index)
        gated = cases["adjudication"][g["ref"]] == GATED_POSITIVE
        out.append(Case("b1_positive", g["ref"], g["positive"], found.text,
                        "SUPPORTED" if gated else "REPORT", ref=where,
                        meta={"adjudication": cases["adjudication"][g["ref"]]}))
        out += [Case("b1_negative", f"{g['ref']} {n['id']}", n["text"], found.text,
                     "NOT_SUPPORTED", repeats=repeats if "hard" in n["kind"] else 1, ref=where,
                     meta={"kind": n["kind"]}) for n in g["negatives"]]
    gold = load("research_integrity/b1_gold_v2.json")["claims"]
    for ref, label in gold.items():
        found, claim = unit(ref)
        out.append(Case("b1_verified", ref, claim, found.text,
                        "SUPPORTED" if label == "SUPPORTED" else "NOT_SUPPORTED",
                        repeats=repeats if label != "SUPPORTED" else 1,
                        ref=(found.observation_id, found.index), meta={"gold": label}))
    return out


def decide(cases: list[Case], rows: list[dict]) -> dict:
    """`rows`: {"set", "id", "repeat", "verdict"} per judgment made."""
    by_case: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        by_case.setdefault((r["set"], r["id"]), []).append(r["verdict"])
    failures, inconclusive, reported = [], [], []
    for c in cases:
        got = by_case.get((c.set, c.id), [])
        what = f"{c.set} {c.id}"
        if c.expected == "REPORT":
            reported.append(f"{what} ({c.meta.get('adjudication')}): {got}")
            continue
        if len(got) < c.repeats:
            inconclusive.append(f"{what}: {len(got)} of {c.repeats} judgments recorded")
        if c.expected == "NOT_SUPPORTED" and "SUPPORTED" in got:
            failures.append(f"{what} was judged SUPPORTED ({got.count('SUPPORTED')} of "
                            f"{len(got)}): {c.statement!r}")
        elif c.expected == "SUPPORTED" and any(v not in ("SUPPORTED", "VERIFICATION_ERROR")
                                               for v in got):
            failures.append(f"{what} (adjudicated supported) was judged {got}: {c.statement!r}")
        if "VERIFICATION_ERROR" in got:
            inconclusive.append(f"{what}: VERIFICATION_ERROR in {got}")
    status = "FAIL" if failures else "INCONCLUSIVE" if inconclusive else "PASS"
    counts = {s: sum(1 for c in cases if c.set == s) for s in dict.fromkeys(c.set for c in cases)}
    return {"status": status, "failures": failures, "inconclusive": inconclusive,
            "reported": reported, "cases": counts, "judgments": len(rows)}
