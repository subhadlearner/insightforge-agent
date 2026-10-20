# T18 final Anthropic verification

Refs #19. Branch `t18-verification` (PR #74, top of the T18 stack #68 to #74). Frozen evaluated
commit: `6cdcf32513348bf1b130e060f74606970dcff102`. Date: 2026-10-21.

**Outcome: T18's live gates are not all met.** Stage C formally FAILED. B-1 FAILED and proceeds
only under a documented exception. B-3 and Stage D were not run. This record reports what the
evidence shows; it does not declare T18 complete or the remediation proven.

## 1. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| Spike criteria 1-3 (Planner thread) | PASS, thin evidence (3 passed, 2026-10-19 03:13Z; no plan text, model IDs or tokens captured; owner accepted without rerun) | evidence package `MANIFEST.md` |
| T18 baseline: five Briefs on Anthropic | All 5 COMPLETE; document and memory paths exercised; fact-check pooled **20/30 = 66.7%** (< 80%, section 11 remediation required); citation accuracy 115/115; judge 2 of 5 scored | baseline `eval-report.md` at `d1b3be8`; immutable, never pooled with later runs |
| B-1 entailment calibration | **FAILED** (3 runs), documented gate exception | `docs/audits/2026-10-21-t18-b1-gate-exception.md` |
| B-2 drafting smoke test | **PASS**: 47 calls, 39 parsed, 8 cut at max_tokens, 0 other failures; cap decision `undecided`, so production cap stays `None` | `stage-b2/b2.json` |
| B-3 judge-only | **Not run** | |
| Stage C targeted end-to-end (2 Briefs) | **FAILED** (acceptance exit 1) | section 2 |
| Stage D full five-Brief re-evaluation | **Not run** (Stage C's stop rule) | |

So the section 11 remediation has been built and partly exercised, but its effect on the
five-Brief fact-check rate has not been measured on Anthropic. The 66.7% baseline is the only
full-library Anthropic result.

## 2. Stage C

Two Briefs, `document-annual-report` and `regulatory-impact-semiconductors`, run with
`insightforge-agent eval --provider anthropic --judge-provider anthropic`, then
`python -m tests.t18_acceptance stage-c`. Claude Haiku 5.5 in every role, judge included;
`draft_structured_output=on`, `draft_passage_cap=None`, `fact_check_method=f2+counterpart-v1`,
`writer_prompt_version=writer-2`.

### 2.1 Formal attempts (in order)

| Attempt | Commit | Runs | Outcome |
|---|---|---|---|
| 1 | `0eb06c7` | `run_d2bd97047785`, `run_600c859dd6fe` | Both FAILED at Report storage: `TypeError: Object of type date is not JSON serializable`. No Report. Acceptance FAILED. Fixed in `d44f459` |
| 2 (rerun) | `d44f459` | `run_61c2b5c7ec63` (`rep_5cb300569eb6f65b`), `run_206596674f75` | Document Report published, 2/13 by the checker of the time; regulatory Run FAILED ("the Writer broke the citation rules after 2 retries"). Acceptance FAILED |
| 3 (final) | `6cdcf32` | `run_3176a540d128` (`rep_40b0318c716fe61e`), `run_72e287c25d3b` (`rep_db6a9f04350bbcf2`) | Both COMPLETE. Acceptance **FAILED, exit 1** |

Each attempt was separately authorised and run once; nothing was retried.

### 2.2 Final attempt (formal result)

2026-10-20 23:13:22 to 23:39:36 UTC.

| | Document | Regulatory |
|---|---|---|
| Run state | COMPLETE | COMPLETE |
| Coverage | **10/13** covered, 2 candidate, 1 missing | **2/2** objectives covered |
| Fact-check (sampled) | 5/5 verified | 9/9 verified |
| Citation accuracy | 20/20 | 45/45 |
| Synthesis judge | 2.75/5 (coverage 3, organisation 3, synthesis 2, calibration 3) | 2.25/5 (3, 2, 2, 2) |

In total 14/14 sampled Claims verified and 65/65 citations accurate. These sample sizes are
small, and two Briefs are not the five-Brief library.

Northwind facts: covered: revenue, growth, the three segments, R&D $71 million, NW-200 pivotal
trial, NW-200 approval expected H1 2026, titanium supplier risk, FY2026 outlook 9-11%.
Candidate: hospital pricing pressure risk, NW-200 delay risk. Missing: 6 programmes in clinical
trials. Regulatory: Nvidia China revenue covered; H20 charge or lost sales covered.

Acceptance reasons, verbatim: `6 programmes in clinical trials: missing`,
`hospital pricing pressure risk: candidate`, `NW-200 delay risk: candidate`.

### 2.3 Remaining issues

1. **Six clinical-trial programmes: pipeline loss.** Passage 4 of the uploaded PDF was
   retrieved for the R&D Sub-task and drafted ("The Northwind Health pipeline holds 6
   programmes ..."), then dropped by the deterministic literal check: `name not in Passage:
   ['Northwind Health', 'Northwind Health pipeline']`. The compound label "Northwind Health
   pipeline" is wider than the document's verified company ("Northwind Health"), so the
   document-identity credit (CS2) did not apply. No judge call was made.
2. **Hospital pricing pressure: checker candidate.** The Claim ("Pricing pressure ... for
   Northwind Health.") names Northwind; the checker then treats any other possible name as
   unsettled. On that path it decides whether "Pricing" is an ordinary word from the Report's own
   wording only, not the Passages the Run read, which do write "pricing". A vocabulary
   inconsistency that fails closed (never a false pass) but here wrongly.
3. **NW-200 delay risk: checker candidate.** "Delays" opens the sentence and no Passage writes
   it in lower case, so the rules cannot rule out a name. A provisional single-reviewer manual
   adjudication (stated by the Claim and its cited Passage) is kept with the evidence; it is not
   a gate result and was not accepted as one.
4. **Regulatory output limits.** 5 drafting batches and 2 extraction batches were cut at the
   4,096-token output limit, and 1 extraction reply was invalid JSON. Their items were lost; the
   two objectives were still covered.
5. **Report quality.** Both judge scores are low. The judge noted unreconciled market CAGR
   figures, AMD China revenue never quantified, a "Gaps & limitations" section stating none
   despite missing items, and lists of disconnected facts.

Also observed: the Writer's Claim fallback replaced 2 document Claims (comparison caveats adding
years or names their Evidence lacked) with their items' statements; that Report then verified
5/5. The regulatory Writer published after its existing summary repair (no Claim replaced).

### 2.4 What changed between attempts (all offline-tested; see PR #74)

| Commit | Change |
|---|---|
| `d44f459` | Report body stored in JSON mode (dated Evidence) |
| `e957c4f`, `dc652d0`, `6cdcf32` | Stage C checker: attribution through the cited Passage of the uploaded report; contributor segments; competitor and digit-code negatives |
| `dc652d0` | Synthesis entailment judges a credited document Passage without the credited company name; conservative Writer Claim fallback; `evidence_entailment` diagnostics |
| `6cdcf32` | The same document-company credit in the Fact-Checker (current document Claims only); forward-guidance drafting instruction |

The final attempt is the first live run of these changes. Its R&D and outlook coverage is
consistent with them working as intended, from one run only.

### 2.5 Retrospective diagnostics (not formal results)

The corrected checker was re-applied offline to attempt 2's archived Report: 10/13 at `e957c4f`,
then 9 covered and 1 candidate at the later checker. These explain checker changes; they do not
replace attempt 2's formal FAILED.

## 3. Cost

| Spend | Figure |
|---|---|
| B-1 runs 1-3, B-2 | Harness estimates from provider-reported tokens: $0.0129, $0.0156, $0.0152, $0.0854. Not billed amounts |
| Stage C attempts 1-3 | **Unknown.** The evaluation CLI records no token usage, and Console balances were not captured |
| Actual Anthropic billing for T18 overall | **Unknown** |

## 4. Evidence

The evidence package is kept outside the repository because it holds third-party web text and
local databases; it should be copied to durable storage. Each stage folder has a `SHA256SUMS`.

| Folder | Content | Status |
|---|---|---|
| `raw/`, `eval-report.*`, `per-brief/`, `run-events/`, `analysis/` | T18 baseline (`eval.db` sha256 `d061f8b1...aff`) | formal |
| `stage-b1/` | B-1 runs 1-3, audit, gate exception | formal |
| `stage-b2/` | B-2 record (`b2.json` `418aa70b...86c0`) | formal |
| `stage-c/` | attempt 1 (failed at storage) | formal |
| `stage-c-rerun/` | attempt 2 (`eval.db` `5e08b238...0fd`) | formal |
| `stage-c-rerun-retrospective/` | re-applied checkers; NW-200 delay-risk manual note | diagnostic only |
| `stage-c-final/` | attempt 3 (`eval.db` `ffa4464a...0afa`, `eval-report.json` `dd6bf18b...7f56`, `acceptance.log` `0c84d63d...d930`) | formal |

## 5. Follow-up (outside the capstone submission)

Prioritised; tracked separately from T18 in #76 (and #75):

1. Deterministic source-identity and compound-entity handling: a model label that extends the
   document's company ("Northwind Health pipeline") should not deny the CS2 credit when the
   statement's names are otherwise the company's.
2. Conservative acceptance-checker improvements: one vocabulary for ordinary words on every
   attribution path; a reviewed path for sentence-initial words.
3. Report synthesis: reconcile conflicting figures, quantify what the Brief asks for, and state
   real limitations in "Gaps & limitations".
4. Model output sizing and extraction completeness: drafting and extraction batches cut at
   4,096 tokens, and invalid JSON replies, still lose items on long web sources.
5. #75: the fact-checker accepting qualifiers stated outside the cited Passage (B-1 finding).
