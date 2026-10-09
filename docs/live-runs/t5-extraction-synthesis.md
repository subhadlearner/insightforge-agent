# T5 live run: Extraction and synthesis

- **Date:** 2026-10-10
- **Code under test:** `7f790d8` (T5 review round 2).
- **Provider and models:** Anthropic `claude-haiku-5-5` for planner, writer and light roles. Search: Tavily.
- **Brief:** "Compare BYD and Tesla in battery electric vehicles: 2025 sales volume, pricing strategy and battery technology."
- **How it was run:** one run, no retries. `tests/live/test_brief_to_report.py` prints only event types, so the same Brief and wiring were run once through a scratch script (not committed) that also printed the T5 numbers below. The test itself was not changed.
- **Not in the repo:** API keys, Source bodies and the raw log.

## Outcome

The Run reached `COMPLETE` and stored one Report with 27 Claims. Planner, Researcher and Writer behaved as in T3/T4 (no plan correction, trim or repair).

| Measure | Value |
|---|---|
| Sources summarised / skipped (paywall, fetch) | 20 / 10 |
| Extracted facts | 129 (115 with entity, predicate and value; 42 with a known period) |
| Extraction conflicts | 0 |
| Evidence items in the bundle / Claims in the Report | 68 / 27 |
| Confidence | LOW 68, MEDIUM 0, HIGH 0 (all 3 sections LOW) |
| Conflicting items / ignored contradictions | 0 / 1 |
| Evidence dropped | 108: 107 failed provenance or literal checks, 1 draft cited no Passage |
| Evidence cut for budget | 0 (the bundle was well under 6,000 tokens) |
| Structured values rejected | 14 of 129 in Extraction, 45 in Synthesis |
| Stated periods rejected | 14 in Extraction, 15 in Synthesis |
| As-of handling (68 items) | 30 stated, 22 derived from the observation date, 16 UNKNOWN |
| Model batches that returned unparseable JSON | 1 extraction, 4 drafting (batch skipped, logged) |
| Provider tokens (all roles, whole Run) | 465,652 total: 359,438 input, 106,214 output (34,315 reasoning), 44,141 cache reads. Cost not computed: no price table for this model is in the repo. |

## Findings

1. **The pipeline holds up end to end on a real model.** No crash, no oversized payload, no dangling citation, and Passage windows and batch rendering behaved.
2. **Confidence is uninformative on this run: all 68 items are LOW.** Items are rarely supported by more than one Source (merging is by exact structured key), and none reached the MEDIUM credibility threshold. Cause not yet established: I did not capture Source credibility scores or per-item supporting counts. This needs a look before the thresholds are trusted.
3. **The literal check drops a lot: 107 of 176 candidates (61%).** Typical reasons in the log:
   - the model declared an "entity" that is a label rather than text in the Passage ("BYD vs Tesla", "China NEV market", "U.S. federal EV tax credit");
   - multiword or sentence-initial names the capitalised-word heuristic checks against a single Passage;
   - years or figures the model took from a different Passage than the one it cited (the check is per Passage).
   These are safe drops (nothing unsupported got in) but they cost recall.
4. **Compound structured values are rejected often**, for example "1,636 million units, down 8.6%" or "about $36-40k vs many global EVs". That is the intended conservative behaviour, though it means those items cannot take part in conflict decisions.
5. **No contradiction was found,** so conflict handling was not exercised by a real contradiction in this run. The one `contradiction_ignored` event was a same-value verdict.
6. **Derived As-of dates are broad:** 22 items took the observation date, including timeless statements (a cell's shape, a charger's rated power). This follows the spec's present-state rule but is the model's call in the entailment step.
7. **Five model replies were unparseable JSON** and their batches were skipped and logged. The log keeps only the start of each reply, so the cause (an output limit on long replies is one candidate) is not established.

## Not done

Single run only, as instructed. No repeat trials, no comparison with another provider, and the live test was not extended.
