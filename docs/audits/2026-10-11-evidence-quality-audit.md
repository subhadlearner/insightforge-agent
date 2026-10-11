# Evidence quality audit (2026-10-11)

**Purpose:** find real grading risks in the research pipeline before more implementation. **Scope:** findings only. No production code changed, no paid call made, M1.6-M5 not reopened.

**Inputs:** the 48-case pilot benchmark run against the production deterministic baseline; the October 10 live run (`docs/live-runs/t5-extraction-synthesis.md`, code `7f790d8`); the current code at `cfa44a0`; the rubric in `assignment_04_market_intelligence.md`.

## Summary

| # | Problem | Class | Severity | Effort |
|---|---|---|---|---|
| F1 | Web Sources never get a publication date, so credibility is flat, MEDIUM can't be reached and citation dates are blank | **MUST FIX** | High | S (about 1 day) |
| F2 | The Fact-Checker fails true Claims (and passed one false Claim), so the 80% pass rate is at risk | **MUST FIX** | High | M (1-2 days, plus one authorised live run) |
| F3 | Five unparseable model replies; the likely cause is the 4,096-token default on the light role | OPTIONAL | Low | XS |
| F4 | A merge key with an UNKNOWN period ignores time (5/5 judged PAIR negatives were merged) | OPTIONAL | Medium (false-HIGH risk), low grading impact | S |
| F5 | 107/176 Evidence candidates are rejected | NO ACTION | Low | - |
| F6 | No real contradiction was seen in the live run | NO ACTION | Low | - |
| F7 | Syndicated copies count as independent Sources | NO ACTION | Low | - |

## F1. No publication date, so credibility and Confidence say nothing (MUST FIX)

- **Problem.** `HttpFetcher.fetch` builds the `Page` with `html_to_page`, and nothing ever sets `published_at` (`agents/web.py:145-170`). `credibility_score` then gives every ordinary domain exactly **0.6**, and `.gov/.edu/.int` 0.8 (`web.py:176-184`). MEDIUM needs at least 0.65 (`domain/evidence.py:19`). Byline and the "major press > blogs" tier are not implemented (the docstring defers them to "the full credibility work", and no open ticket owns that work: ledger row 15).
- **Evidence.**
  - Live run: LOW 68, MEDIUM 0, HIGH 0. The run log says "cause not yet established", but the cause is deterministic. Today's wiring cannot produce anything else for a single commercial Source.
  - Offline probe of the real function: `reuters.com` and `electrek.co` both score 0.6 when undated and 0.7 when dated within a year.
  - The existing test `tests/domain/test_confidence_matrix.py::test_an_undated_ordinary_domain_cannot_reach_medium_with_the_current_wiring` already records this.
- **Grading impact.**
  - Web Search (rubric section 3): the credibility rubric (recency decay, byline, domain type) is only partly met.
  - Writer: the inline citation `[Source: {domain}, {date}, credibility: {score}]` would show no date and the same score every time.
  - Synthesis: "ranks by recency first" falls back to stated periods only.
  - Report UI: every badge is red. All of this is visible in a manual review of the 5 sample Reports (Report quality, 20%).
- **Fix.** Read the publication date in `html_to_page` (`article:published_time` / `og:` meta, JSON-LD `datePublished`, `<time datetime>`), add the byline (`meta[name=author]`, JSON-LD `author`) and a small major-press allow-list to `credibility_score`, and update the confidence-matrix test. Keep the thresholds as they are.
- **Effort.** S. One module plus its tests, no new dependency.

## F2. The Fact-Checker fails true Claims and can pass false ones (MUST FIX)

- **Problem.**
  - `fact_check` compares a one-sentence Claim with whole re-fetched paragraphs, using the bag-of-words `HashingEmbedder(256)` at 0.85 (`pipeline/fact_check.py:36-110`, `pipeline/deps.py:36`).
  - Cosine between a sentence and a paragraph falls roughly with the square root of the paragraph's length, and any paraphrase by the Writer lowers it further.
  - The rubric asks for semantic similarity via Qdrant. The semantic embedder (`fastembed`) is planned for T7/T8 and is not wired.
- **Evidence.**
  - Benchmark (CLAIM_PASSAGE, 2 cases): **0/2 correct**.
    - `g-qu-02`: a true Claim ("4.27 gigawatt-hours" against "4.27 GWh") scored 0.639, so `unverified`.
    - `g-ma-02`: a false Claim ("R&D spend was $90 million" when $90 million is the revenue) scored 0.864, so `verified`.
  - Offline probe with the production embedder (`scratchpad/probe_similarity.py`):
    - A Claim copied word for word, scored against a paragraph of n sentences, gets 1.000, 0.802, 0.659, 0.607 and 0.585 for n = 1 to 5. Only a one-sentence paragraph passes.
    - Three ordinary Writer paraphrases of a one-sentence Passage scored 0.819, 0.772 and 0.683, so all three fail.
  - **No live pass rate exists.** The October 10 run was at `7f790d8`, where FACT_CHECKING was still a pass-through (T6 merged later in `2667ec7`). The run says nothing about real verification.
- **Grading impact.**
  - Fact-check pass rate (15%) requires at least 80% of the checked Claims to pass, and `[UNVERIFIED]` applied correctly. On this evidence most true Claims taken from multi-sentence web paragraphs will be flagged `[UNVERIFIED]`, which also hurts Report quality (20%).
  - T18 and `docs/design.md` (eval section) already say that a rate under 80% triggers remediation. This finding says to expect that.
- **Fix.**
  1. Score the Claim against each sentence (or two-sentence window) of the re-fetched Passage, and keep the numeric check on the matched span.
  2. Bring forward the `fastembed` embedder that T7 already plans, behind the existing `Embedder` interface.
  3. Then make **one owner-authorised live run** to measure the pass rate.

  Step 1 changes how a ledger-row-13 approved criterion is applied (the threshold stays 0.85), so it needs the owner's explicit approval as a design change.
- **Effort.** M. Step 1 is small (S). Step 2 adds the planned dependency early. Step 3 is paid.

## F3. Five unparseable model replies (OPTIONAL)

- **Problem.** 1 extraction batch and 4 drafting batches returned invalid JSON. Each batch was skipped and logged, with no retry (`agents/llm_json.py`).
- **Evidence.** The light role uses the `ChatAnthropic` default `max_tokens=4096` (checked offline: `ChatAnthropic(model="claude-haiku-5-5").max_tokens == 4096`, langchain-anthropic 1.7.5). Only the writer was raised to 16,000 (`llm.py:31-33`), after the T3 live run found the Writer's JSON cut off at exactly this default. The October 10 run used 34,315 reasoning tokens, and reasoning shares that budget. Truncation is the leading hypothesis but is **not established**: the log keeps only the first 200 characters of each reply.
- **Grading impact.** Low. The batches fail safe, and the Report still had 27 Claims. The cost is some lost recall.
- **Fix.** Log the stop reason and the end of the reply. If that confirms truncation, give the light role the same `max_tokens` setting as the writer.
- **Effort.** XS.

## F4. Merging on an UNKNOWN period (OPTIONAL)

- **Problem.** `merge_identity` keys a candidate by entity, predicate, scope, period and value. `stated_period` turns any period it cannot read into `UNKNOWN`, and two UNKNOWN periods are equal, so the dates are ignored.
- **Evidence.** The baseline merged **5 of the 5 judged PAIR negatives** (all `DIFFERENT_FACT`):
  - `p-ta-02`: present tense, published in 2021 and 2025.
  - `p-ta-03`: "this year", in 2023 and 2024.
  - `p-ps-01`: "fiscal 2023" vs "fiscal 2024".
  - `p-ps-04`: "end of 2022" vs "end of 2024".
  - `p-fm-02`: a same-named, different company.

  `p-ps-01` and `p-ps-04` only arise if the model ignores the prompt's period format. `p-ta-02` and `p-ta-03` are realistic. Nothing in the live run shows such a merge.
- **Grading impact.** Low today. A false merge across two domains could produce a false HIGH that a grader is unlikely to notice. It matters more once F1 makes Confidence informative.
- **Fix.** Do not merge structured candidates from different Observations when the period is UNKNOWN. The deterministic baseline changes as a result, so record it for the #26 comparison.
- **Effort.** S.

## F5-F7. Acceptable behaviour (NO ACTION)

- **F5. 107/176 rejected.** The reasons in the log (a model label used as an entity, such as "BYD vs Tesla"; a figure taken from a Passage the item did not cite) are correct provenance drops. On the benchmark the literal check dropped 6 of the 10 SUPPORT negatives and 0 of the 2 positives. It also dropped two true QUOTED_SPAN items (`g-qu-01` "2024" only in the caption, `g-qu-03` "US$2.4bn"), which is lost recall but not unsafe. 68 items and 27 Claims are enough for a Report.
- **F6. No contradiction in the live run.** Nothing is wrong. The path is exercised offline: `insightforge-agent demo` shows the contradicting Tesla figures as conflicting and LOW. A sample Brief that is likely to surface a conflict belongs to T16.
- **F7. Syndication.** `i-sy-02` (wire copy on another domain with no link) was judged INDEPENDENT. This is the documented lexical limitation (methodology, Limitations). Neither the rubric nor T5 asks for syndication detection.

## Benchmark results by category (deterministic baseline, 48 reviewed cases)

| Group | Judged / eligible | Result |
|---|---|---|
| **False merge** (PAIR, all) | 7 / 24 | 5 of 5 judged negatives merged (FPR 1.000); 2 of 8 SAME_FACT merged correctly |
| - false_merge_negative / period_scope_mismatch / temporal_ambiguity | 1/6, 2/4, 3/3 | 1, 2 and 2 false merges (see F4) |
| - contradiction / alias_predicate_synonym | 0/4, 0/4 | NOT_APPLICABLE (a nonmerge says nothing about which) |
| **False contradiction** | 7 PAIR judged | 0 |
| **False support** (SUPPORT) | 0 / 12 | NOT_APPLICABLE by design (support is a model verdict); the literal check dropped 6/10 negatives and 0/2 positives |
| **Grounding**: CLAIM_PASSAGE (T6 `fact_check`) | 2 / 2 | 0/2 correct: 1 false verify, 1 true Claim failed (F2) |
| **Grounding**: QUOTED_SPAN (literal check) | 3 / 6 | `g-qu-01`, `g-qu-03` wrongly NOT_GROUNDED (recall); 1 correct reject |
| **Independence** | 4 / 4 | 3/4; 1 false independence (`i-sy-02`, F7) |
| Not evaluated | - | false-HIGH Confidence, budget cuts (the cases cannot carry them) |

The baseline is a floor. Its many NOT_APPLICABLE answers are by design, not defects. Each category has only 2-6 cases, so these figures describe what happened and prove nothing statistically.

## Not graded risks here

Docling, Qdrant, the UI, the scheduler, the Markdown rendering of citations and the Executive Summary belong to T7-T16 and are not assessed. F1's citation-date gap will show up in whichever ticket renders the citations.

## Offline verification (2026-10-11, branch `audit-evidence-quality` at `cfa44a0`)

| Command | Result |
|---|---|
| `uv run insightforge-agent bench baseline --cases benchmarks/evidence/cases.jsonl --recordings <scratch>/rec --run audit-2026-10-11` | exit 0, QUALIFICATION report, 48 eligible, 0 excluded |
| `uv run lint-imports` | 4 contracts kept, 0 broken |
| `uv run pytest -m baseline` / `-m replay` / `-m scripted` | 20 / 76 / 211 passed |
| `uv run pytest` (all non-live) | 1037 passed, 5 deselected (live) |
| `uv run insightforge-agent demo` | exit 0, 7/7 PASS |
| `uv run python <scratch>/probe_similarity.py` | figures quoted in F2 (production `HashingEmbedder` + `cosine`, read only) |

No live test, no paid call. The baseline recordings and the probe script are in the session scratchpad, not the repo.
