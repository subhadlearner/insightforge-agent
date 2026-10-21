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

| Case | Kind | full map | sentence map | lazy map | identity abuse | Old gate |
|---|---|---|---|---|---|---|
| NW-P01 | six programmes (Stage C loss) | SUPPORTED with honest map | | | | **refused** (false negative) |
| NW-N01 | wrong amount | code | code | | code | refused |
| NW-N02 | wrong company, identity context | model | model | | code | refused |
| NW-N03 | wrong period | code | code | | code | refused |
| NW-N04 | expected stated as granted | **code** (was model) | code | | | passed |
| NW-N05 | added segment qualifier | model | model | code | model | passed |
| NW-N06 | negation | **code** (was model) | code | | | passed |
| NW-N07 | competitor sentence in own report | model | model | | model | **refused** (trade-off, §8) |
| WEB-N01 | competitor's figure in same Passage | model | model | | code | passed |
| WEB-N02 | wrong magnitude | code | code | | | refused |
| Q-N01 | B-1 `1.5` market definition | model | model | code | | passed |
| Q-N02 | B-1 `2.9` capex | code\* (was model) | code\* | code | | passed |
| Q-N03 | B-1 `3.3` solid-state | model | model | code | | passed |
| Q-N04, Q-N05 | new: denominator, market scope | model | model | code | | passed / refused |

`support-gold-2` (PR #77 review): floors re-measured after support check 5 (logical operators); the `sentence` persona quotes only the Passage's best-matching sentence. NW-N04 and NW-N06 are genuine new floors (the quote's "expects" and the statement's "not" are what make them false). \*Q-N02's is coincidental: "planned" in the same sentence governs another figure ("$175-185 billion"), not the capex claim, so it is not relied on. Every negative is now repeated in the live gate whatever its floor. No `expected` label changed.

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
| After the review corrections: `T18_EVAL_DB=<baseline eval.db> pytest -m integration` over the six T18 replay files (gate plan, acceptance baseline, counterpart, document, provenance, Stage B setup) | 11 passed, 1 skipped (acceptance baseline needs `T18_EVAL_DIR`); `eval.db` hash unchanged. `test_t18_stage_b_setup.py` still imported the removed `entailment_request` (broken since `70cfdba`, missed by the earlier replay run); ported to `render_request` over the Fact-Checker's own units |
| `pytest tests/pipeline/test_research_integrity_review.py` at `86584c2` / after | 19 failed, 3 passed (controls) / 24 passed (2 tests added with the corrections: stated limits stay in the summary; the token cap still stops the Run) |
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
8. **Summary.** Since the PR #77 review, each sentence that is not a published Claim verbatim is judged by the support contract against the published Claims (not against Passages directly), and removed if not SUPPORTED. A strict judge removes abstractive sentences, so live summaries may often be rebuilt from whole Claims (flagged). A figure-free sentence saying what this research did not establish is kept unjudged (it states no finding); an assertion that a source lacks something is now removed unless the Claims support it, while the T18 absence flag still only measures.
9. **Same model, same contract.** Synthesis and the sampled Fact-Checker use the same judge; the sample is not an independent oracle.
10. **Intermediate commits** are review slices; the suite is verified at the branch head.
11. **Operator word lists** (support check 5). Negation, hedging, exclusivity, universality and comparison are fixed word lists; an operator worded otherwise ("failed to", "outpaced") is the model's to judge, and a faithful paraphrase that drops one is refused. A genuine quote attached to a part it does not state is not caught by code (only the live gate measures it).
12. **Answer check and Brief coverage are model and lexical.** Whether an item answers a requirement is a light-model judgment with a code-checked quote; whether the ledger covers the Brief is a lexical term check. Neither proves the decomposition complete; Gate B's human review must.
13. **More calls before publication.** One support call per reworded Claim, one joint call per summary sentence not reused verbatim, and up to one answer call per requirement with linked items (at most 12) per Run.

## 9. Proposed live validation (NOT authorised; nothing run)

| Gate | What | Provider and model | Calls | Estimated spend | Hard bound | Attempts |
|---|---|---|---|---|---|---|
| **A. Support gate** | `tests/live/test_research_integrity_live.py`: 20 gold cases (`support-gold-2`), 10 + 40 B-1 cases, 27 historical Claims; prompt `support-2`; 3 repeats of every gold negative and of the hard B-1 negatives (159 judgments, recomputed read-only from the baseline `eval.db` after the review) | Anthropic, light model (`claude-haiku-5-5`), SDK retries off | 159 | ≈ $0.03-0.07 expected; ≤ about $0.35 if every reply hit `max_tokens` | `Budget` guard stops before any call whose worst case would pass `RESEARCH_GATE_BUDGET_USD` (required, ≤ $1.00). **Proposed: $0.50** | 1 |
| **B. Stage-C-equivalent** | `insightforge-agent eval --provider anthropic --judge-provider anthropic --brief document-annual-report --brief regulatory-impact-semiconductors`, new output folder; never pooled with T18 Stage C | Anthropic (all roles) + Tavily search | ≈ 80-200 model calls (T18 Stage C usage was not recorded; the review corrections add per-Claim, per-summary-sentence and per-requirement light calls) | **Unknown**; T18 Stage C cost was not recorded | **None available**: `RUN_TOKEN_CAP` covers Passage-reading inputs only | — |

**Ready for new paid validation: Gate A yes; Gate B no.** Gate A is bounded by an enforced pre-call guard and decided by a rule fixed in advance (`tests/research_integrity_gate.py`, unit-tested; dry run passes). Gate B has no hard spending bound, and its acceptance oracle needs new human adjudication: the T18 Stage C checker reuses production provenance code, so it is not independent of the code under test. Per the handover's rule, Gate B should not be launched until a hard cap exists or the owner explicitly accepts a soft one, and only after Gate A passes. Stage D stays not authorised.

## 10. Independent review of `86584c2` and the corrections

An independent review of PR #77 at `86584c2` reported six P1 and two P2 findings. Each was checked against the code at that head before anything changed; all eight described real paths, and all were corrected. Two further defects surfaced while correcting them and were fixed: the summary check would have removed honest "this research did not establish" sentences (now kept unjudged when they hold no figure), and broad exception handlers in the new checks would have swallowed the Run's token-cap stop (`RunFailed` now propagates). For each, `tests/pipeline/test_research_integrity_review.py` adds an adversarial regression test (a scripted model giving the wrong or broken answer). At `86584c2` the file gave **19 failed, 3 passed** (the 3 are controls: a supporting citation still verifies, an exact restatement needs no second call, a single sentence is not split); after the corrections, 22 passed.

| # | Finding | Verdict | Correction | Regression tests | Residual |
|---|---|---|---|---|---|
| P1 | Fact-Checker verified on support at another Passage of the same Source | Valid (`fact_check._verdict` took the first SUPPORTED candidate) | Only the cited Passage (stored chunk or exact counterpart) verifies; support elsewhere → unverified, `matched` names it, `fact_check_citation_mismatch` logged; a drifted page verifies only if the as-cited Passage supported the Claim; a premise with no counterpart → unverified | `test_support_elsewhere_on_the_page_never_verifies…`, `test_the_same_holds_for_an_uploaded_document`; F2 test inverted (`test_a_supported_span_of_another_passage_never_verifies_the_cited_one`) | Lower pass rate where Writers cite a weaker Passage; the citation is never re-pointed automatically |
| P1 | Ordinary reworded Claims published without a semantic check | Valid (only premises were judged; lexical rules pass "increased" for "declined") | Every Claim not equal to its item's verified statement is judged against the item's primary Passage before publication; failure → restated verbatim (or removed if a duplicate), flagged | `test_a_claim_that_reverses_its_evidence_is_never_published` (both review examples), `test_a_faithful_rewording_is_judged_and_kept`, `test_an_exact_restatement_needs_no_second_judgment` | One light call per reworded Claim; the judge is the same model as Synthesis |
| P1 | Requirement ledger counted an item for a requirement on its id alone | Valid (`assess` trusted Extraction's links) | `confirm_answers`: one call per requirement asks which linked items answer it, with a quote code finds in the statement; unconfirmed items do not count; a failed check → `verification_incomplete`. Ledger derivation: Brief terms no requirement mentions are sent back once, then recorded (`uncovered_brief_terms`) | `test_a_valid_but_wrong_requirement_id_does_not_answer…`, `test_a_failed_requirement_check_is_verification_incomplete…`, `test_an_answer_quote_that_is_not_in_the_statement…`, `test_brief_terms_no_requirement_covers…` | The answer is a model judgment; Brief coverage is lexical; completeness needs human review (Gate B) |
| P1 | Negation, exclusivity, comparison and modality treated as stopwords | Valid (`not`, `no`, `only`, `all`, `any`, `will`, `may`... were function words) | Operators are content words, and support check 5 holds them to the quotes (both ways for negation and hedging, including a cut-off "did not"/"expected to"); prompt `support-2` asks to quote them | `test_an_omitted_negation…`, `test_a_negation_trimmed_off_the_quote…`, `test_a_hedge_trimmed_off_the_quote…`, `test_only_and_all_must_be_stated…`, `test_the_operators_are_held_in_a_joint_judgment…` | Word lists (§8.11); a genuine but irrelevant quote is not caught offline and must be measured live |
| P1 | A failed support call looked like insufficient Evidence | Valid (dropped as `DROP_UNSUPPORTED`) | `DROP_UNVERIFIED` when a Passage that could support the item was never judged; the drop carries Aspect and requirement ids; the ledger gives `verification_incomplete` with its own gap wording; Sub-task gap lines count it separately | `test_a_broken_support_reply_is_verification_incomplete_through_to_the_ledger` | Only failures inside the support call; a Sub-task that failed earlier stays `incomplete` |
| P1 | A rejected analytical conclusion could survive in the summary | Valid (summary checked lexically against the bundle) | Every summary sentence not reused verbatim from a published Claim is judged against the published Claims; failures removed, summary rebuilt from Claims if empty, flagged | `test_a_rejected_comparison_is_removed_from_the_summary_too`, `test_a_summary_sentence_the_published_claims_do_not_support_is_removed` | Strict: abstractive sentences may be removed (§8.8) |
| P2 | Ledger final before Fact-Checking | Valid | `after_fact_check` records each sampled Claim's outcome per requirement; all answering Claims unverified → `reported_unverified`, its own gap line and quality flag; page drift and unchecked recorded but never a failed citation | `test_a_requirement_whose_claims_all_failed_fact_checking…`, `test_the_stored_ledger_and_gaps_show_a_failed_fact_check` (end to end) | Sampled Claims only (20%, minimum 5) |
| P2 | A single long Passage could not be split after truncation | Valid | A one-Passage batch is split by window, then by sentence, within the same depth and retry bounds; each piece cites the Passage and Synthesis judges against the whole Passage | `test_a_single_long_passage_is_split_at_sentences…`, `test_a_single_sentence_still_cannot_be_split` | A single over-long sentence is still exhausted and reported |

Gate-level questions the review raised, answered as far as offline work can:

1. **Does the support-map model judge the adjudicated B-1 cases reliably?** Not knowable offline. That is Gate A's purpose; it is unchanged in scope, now with `support-2`, `support-gold-2` and every gold negative repeated (159 judgments).
2. **Does the requirements ledger represent the whole Brief?** Code now sends uncovered Brief terms back once and records the rest on the ledger, and confirms answers per requirement. Neither proves completeness: Gate B's oracle must include a human check of each Brief against its ledger.
3. **Is the Stage C acceptance oracle independent of the new architecture?** No. The T18 Stage C checker reuses production provenance code, and the offline Stage C fixture here uses scripted honest models. Gate B needs a new human-adjudicated oracle before it can measure anything; it stays not ready.

Results after the corrections are recorded in the PR description with the commit SHA.

