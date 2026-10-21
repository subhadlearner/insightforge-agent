# Research-integrity redesign (ADR-0006): offline verification record

- **Branch:** `fix/t18-research-integrity`, based on `t18-verification` at `315288e6f0eea6c19f58fe23649ffb2df5c5c5f9` (verified: local and `origin/t18-verification` agree; the handover's copy of this hash was 39 characters, the same commit). Draft PR into `t18-verification` (stack #68 → #74). Refs #19, #75, #76.
- **Date:** 2026-10-21. **Mode:** offline only. **Paid calls made: none. Spend: $0.**
- **Status:** implementation complete for the scope below; **live validation not run** (needs separate authorisation, section 9). This is not a claim that the capstone, T18 or the redesign is verified.

## 1. Preserved evidence

The T18 record is unchanged: `docs/t18-verification.md`, the two B-1 audits, `tests/t18_acceptance.py`, the B-1/B-2 fixtures and all archived artifacts are untouched. Archive checked read-only with `sha256sum -c SHA256SUMS`:

| Folder (`C:\Code\CodeBasics\Assignments\insightforge-t18-evidence\`) | Entries | Result |
|---|---|---|
| `baseline/` (incl. `eval.db` `d061f8b1…9aff`) | 2 | OK |
| `preflight/` | 3 | OK |
| `stage-b3/` | 11 | OK |

Not present on this machine (one drive, `C:`, searched): `stage-b1/`, `stage-b2/`, `stage-c/`, `stage-c-rerun/`, `stage-c-final/`. The Stage C traces used here come from the committed record (`docs/t18-verification.md` §2.3), the deterministic sample PDF (`samples/documents/make_sample_documents.py`), the T18 Northwind chunks committed in `tests/pipeline/test_t18_document_recovery.py`, and the baseline `eval.db` (opened read-only; hash unchanged afterwards). A replay of the Stage C final `eval.db` was therefore not possible.

## 2. Root causes, confirmed against the code at `315288e`

| # | Cause | Where | Effect seen |
|---|---|---|---|
| R1 | A lexical name rule decided support before the judge | `synthesize.literal_problem` → `provenance.missing_names` / `credited_by_document` | Stage C six programmes dropped (`name not in Passage: ['Northwind Health', 'Northwind Health pipeline']`); no judge call |
| R2 | The judge was never made to show where each qualifier is stated | `entailment.ENTAIL_SYSTEM`, one verdict per Passage | B-1 `2.9` SUPPORTED in all three runs; `1.5`, `3.3` inconsistent (#75) |
| R3 | Two passes proposed facts independently; only drafting fed the Report; extraction capped at 3 facts per Passage | `extract.SYSTEM`, `synthesize._draft` | Divergent fact sets; drafting losses invisible to Extraction |
| R4 | Lost batches returned as empty | `extract` (`continue`), `synthesize._draft` (`return []`) | Regulatory Run: 5 drafting + 2 extraction batches cut, 1 invalid, items silently lost |
| R5 | Completeness tracked per Sub-task; gap lines only for Sub-tasks with no item | `write.research_gaps` | "Gaps & limitations" said nothing while requested facts were missing |
| R6 | Similarity floor stopped the cited Passage reaching the judge (CS1A patched only the empty case) | `fact_check.screen` | 11 T18 Claims unverified below the floor |
| R7 | Budget cut by rank only (last-of-Aspect protection only) | `evidence.compress_to_budget` | A unique answer could be cut |
| R8 | One evidence id per Claim; Writer told to restate items | `contracts.Claim`, `write.SYSTEM` | Bullet lists; comparisons uncitable (judge 2.25-2.75/5) |
| R9 | Independence by domain only | `evidence.sources_independent` | Syndicated copies could give HIGH |

## 3. What changed (ADR-0006)

| Contract | Files |
|---|---|
| Evidence-bound support (map checks, identity-only context, VERIFICATION_ERROR) | `domain/support.py`, `pipeline/support.py` |
| One Evidence pipeline (Extraction proposes, Synthesis verifies) | `pipeline/extract.py`, `pipeline/synthesize.py` |
| Work units with bounded recovery | `pipeline/work_units.py`, `pipeline/deps.py`, `config.py` |
| Requirements ledger | `domain/requirements.py`, `pipeline/requirements.py`, `pipeline/graph.py` |
| Writer: requirement index, premises, joint verification, quality flags | `pipeline/write.py` |
| Fact-Checker: cited Passage always judged, citation truth vs drift, joint check | `pipeline/fact_check.py` |
| Citations, export, UI for premises; no Finding from analytical Claims | `pipeline/citations.py`, `pipeline/report_delivery.py`, `scheduler/watchlist.py`, `web/components/citation-drawer.tsx`, `web/lib/api-schema.d.ts` |
| Syndication not independent; budget keeps required answers | `domain/evidence.py` |

## 4. Fixtures and gold sets (independent of production heuristics)

- `tests/fixtures/research_integrity/support_cases.json` (`support-gold-1`): 6 positives/ambiguous and 14 negatives with trace provenance (Stage C, B-1 audit, constructed). Each records the retired gate's verdict and the **deterministic floor**: which permissive-judge personas code refuses alone.
- `tests/fixtures/research_integrity/b1_gold_v2.json` (`support-gold-b1-v2`): the 27 T18-verified Claims, 24 SUPPORTED / 3 INSUFFICIENT_EVIDENCE (`1.5`, `2.9`, `3.3`), extracted mechanically from the accepted audit table (audit SHA-256 recorded in the file). The old B-1 fixtures and gate are unchanged.
- Stage C oracle (`tests/pipeline/test_research_integrity_stage_c.py`): the 13 Northwind facts as hand-written sentences plus the source sentence each cited Passage must contain; exact-text checks, no production code.

### 4.1 Deterministic floor (measured; `tests/domain/test_support_contract.py`)

"code" = refused whatever the model says; "model" = only a correct judgment refuses it.

| Case | Kind | full map | lazy map | identity abuse | Old gate |
|---|---|---|---|---|---|
| NW-P01 | six programmes (Stage C loss) | SUPPORTED with honest map | | | **refused** (false negative) |
| NW-N01 | wrong amount | code | | code | refused |
| NW-N02 | wrong company, identity context | model | | code | refused |
| NW-N03 | wrong period | code | | code | refused |
| NW-N04 | expected stated as granted | model | | | passed |
| NW-N05 | added segment qualifier | model | code | model | passed |
| NW-N06 | negation | model | | | passed |
| NW-N07 | competitor sentence in own report | model | | model | **refused** (trade-off, §8) |
| WEB-N01 | competitor's figure in same Passage | model | | code | passed |
| WEB-N02 | wrong magnitude | code | | | refused |
| Q-N01..N03 | B-1 `1.5`, `2.9`, `3.3` qualifier leaks | model | code | | passed |
| Q-N04, Q-N05 | new: denominator, market scope | model | code | | passed / refused |

## 5. Commands and results

All offline; provider keys unset in the shell; `tests/network_guard.py` fails any outbound connection.

| Command | Result |
|---|---|
| `uv run --frozen pytest -q` at `315288e` (before) | 2369 passed, 37 deselected |
| same, at the branch head | see §5.1 (filled at commit) |
| `pytest tests/pipeline/test_research_integrity_synthesis.py` against the pre-change pipeline (`c36c521` + test) | **7 failed** (red): six programmes dropped, no support call made |
| same after the change | 7 passed |
| `T18_EVAL_DB=<baseline eval.db> pytest -m integration tests/integration/test_research_integrity_gate_plan.py` | 3 passed: all 97 gate cases resolve their cited Passage; 27 = 24 + 3 |
| `uv run insightforge-agent demo` | 7/7 checks PASS |
| `npx vitest run` (web) / `npx tsc --noEmit` | 205 passed / clean |
| `pytest tests/live/test_research_integrity_live.py -m live` (no opt-in) | 1 skipped (never runs without `INSIGHTFORGE_ALLOW_LIVE_TESTS=1`) |

### 5.1 Full suite at the branch head

Recorded in the PR description with the commit SHA (the suite takes about ten minutes).

## 6. Traceability matrix

Classes: **FN** production false negative; **FP** semantic false positive; **GL** incorrect gold label; **RL** retrieval or lost work; **CK** checker-only ambiguity; **SQ** synthesis quality.

| Issue | Class | Old behaviour | New behaviour | Evidence | Residual risk |
|---|---|---|---|---|---|
| Six programmes (Stage C) | FN | Dropped by name rule; no judge | Judged with identity context; kept with an honest map | `test_research_integrity_synthesis.py` (red→green), Stage C oracle 13/13 | Live judge must use identity correctly |
| Hospital pricing pressure | CK | Production kept it; checker said "candidate" | Adjudicated SUPPORTED (gold NW-P03); kept and reported | gold + Stage C oracle | Old checker untouched; it would still say candidate |
| NW-200 delay risk | CK | Same | Adjudicated SUPPORTED (NW-P04) | same | same |
| B-1 `1.5` market definition | GL + FP | Historical VERIFIED; judge inconsistent | Gold INSUFFICIENT; a map omitting the qualifier is refused by code | `support_cases` Q-N01, `b1_gold_v2` | A judge that maps the qualifier to the wrong words is not caught offline |
| B-1 `2.9` capex | GL + FP | SUPPORTED in 3/3 runs | Same as above (Q-N02) | same | same; must be repeated live |
| B-1 `3.3` solid-state | GL | Title-supplied qualifier | Title no longer shown to Extraction or the judge; Q-N03 | same | |
| Context leakage via title | FP | Extraction saw the Source title | Never shown; identity context only for verified PDFs | `test_extraction_reads_one_source_at_a_time…`, `test_extraction_is_told_the_subtasks…` | |
| Truncated / invalid batches | RL | Logged, returned empty | Split and retried (≤2 levels, ≤12 per Run); unread Passages reported | `test_research_integrity_work_units.py` (8) | Support calls are not split (an item is lost, recorded) |
| Drafting vs extraction divergence | RL | Two independent fact sets | One set; extraction uncapped | `test_extraction_synthesis.py`, F4 port | More facts → more support calls (cost) |
| Cosine as verdict | FN | Cited Passage under 0.85 not judged (except CS1A empty case) | Always judged; similarity orders | `test_f2_counterpart.py`, `test_f2_fact_check_entailment.py` | |
| Page drift vs citation | — | Not distinguishable | `cited_support` recorded; drift named | `test_when_the_page_does_not_hold_it…` | One extra unit per drifted sampled Claim |
| "No gaps" while facts missing | SQ | Sub-task-level gaps only | Requirement gap lines; lost work lines | `test_requirements_ledger.py`, Stage C oracle (R5 incomplete) | Requirement derivation quality is live-unproven |
| Unique answer cut by budget | RL | Rank-only | Last support of a required requirement kept; forced cuts logged | `test_a_tight_budget_cuts_extra_items…` | Budget can still force a cut (logged) |
| Disconnected bullets / no comparison | SQ | One item per Claim | Premises; joint verification; requirement index | `test_research_integrity_analysis.py` (12) | Synthesis quality needs live run + human review |
| Writer Claim fallback | SQ | Silent verbatim replacement | Flagged in `ReportDraft.quality` | `test_when_retries_run_out_repair_answers_it…` | Still allowed (as degraded output) |
| Unreconciled CAGR figures; AMD China revenue unquantified | SQ | No mechanism | Writer told to reconcile in an analytical Claim; each side of a comparison a requirement | prompt pins; ledger tests | Not demonstrable offline |
| Syndicated corroboration | FP | Different domains = independent | Same text = not independent | `test_the_same_story_on_two_domains…` | Rewritten copies still pass |

## 7. Old vs new traces (scripted)

- **Six programmes.** Old: `evidence_dropped {reason: "no Passage passes the provenance and literal-value checks", rejected: ["name not in Passage: ['Northwind Health', 'Northwind Health pipeline']"]}`, no entailment call. New: one `support_judged` call showing `Passage [1] is from an uploaded document of Northwind Health`; judgment `SUPPORTED`, identity `["Northwind Health"]`, one excerpt span covering the sentence (offsets only, no text, in the log); item kept.
- **`1.5` with a lazy map.** Old: the verdict alone decided. New: `INSUFFICIENT_EVIDENCE`, problem `no part covers ['cloud', 'infrastructure', 'service']`.
- **Fabricated quote.** New: `VERIFICATION_ERROR`, `excerpt not in the cited text`.
- **Truncated extraction.** Old: `extraction_batch_failed`, facts lost. New: `work_unit truncated → split → parsed, parsed`; all 8 facts kept.

## 8. Known limits, regressions and trade-offs

1. **Live behaviour unproven.** Scripted judges show code paths only. Whether Haiku follows the support map contract (exact quotes, complete parts, identity rules) is unmeasured. A model that misquotes lowers recall (fails closed).
2. **NW-N07 trade-off.** The retired gate refused a competitor's sentence in the company's own report lexically; now only the judge does.
3. **Names are the judge's.** No lexical name check backs the judge (by design; the old check caused R1). B-1 showed 0/40 such negatives falsely supported, from one model and one run each.
4. **Strict coverage.** A part paraphrased instead of copied fails closed (recall loss, logged).
5. **Number words.** `six` in a statement and `6` in a Passage pass; `6` in a statement and `six` in a Passage fail the figure check.
6. **Cost.** Support replies carry maps (longer output); Extraction is uncapped; the Fact-Checker may add one as-cited unit. Only Passage-reading inputs count against `RUN_TOKEN_CAP`.
7. **No arithmetic.** Derived numbers are forbidden rather than computed in code.
8. **Summary.** Still checked lexically, not by the support contract.
9. **Same model, same contract.** Synthesis and the sampled Fact-Checker use the same judge; the sample is not an independent oracle.
10. **Intermediate commits** are review slices; the suite is verified at the branch head.

## 9. Proposed live validation (NOT authorised; nothing run)

| Gate | What | Provider and model | Calls | Estimated spend | Hard bound | Attempts |
|---|---|---|---|---|---|---|
| **A. Support gate** | `tests/live/test_research_integrity_live.py`: 20 gold cases, 10 + 40 B-1 cases, 27 historical Claims; 3 repeats of every negative code cannot stop alone (153 judgments) | Anthropic, light model (`claude-haiku-5-5`), SDK retries off | 153 | ≈ $0.03-0.06 expected; ≤ $0.33 if every reply hit `max_tokens` | `Budget` guard stops before any call whose worst case would pass `RESEARCH_GATE_BUDGET_USD` (required, ≤ $1.00). **Proposed: $0.50** | 1 |
| **B. Stage-C-equivalent** | `insightforge-agent eval --provider anthropic --judge-provider anthropic --brief document-annual-report --brief regulatory-impact-semiconductors`, new output folder; never pooled with T18 Stage C | Anthropic (all roles) + Tavily search | ≈ 60-150 model calls (T18 Stage C usage was not recorded) | **Unknown**; T18 Stage C cost was not recorded | **None available**: `RUN_TOKEN_CAP` covers Passage-reading inputs only | — |

**Ready for new paid validation: Gate A yes; Gate B no.** Gate A is bounded by an enforced pre-call guard and decided by a rule fixed in advance (`tests/research_integrity_gate.py`, unit-tested; dry run passes). Gate B has no hard spending bound, and its acceptance oracle needs new human adjudication: the T18 Stage C checker reuses production provenance code, so it is not independent of the code under test. Per the handover's rule, Gate B should not be launched until a hard cap exists or the owner explicitly accepts a soft one, and only after Gate A passes. Stage D stays not authorised.
