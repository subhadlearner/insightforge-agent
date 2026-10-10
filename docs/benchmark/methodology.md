# Benchmark methodology (PROPOSED)

**Status: PROPOSED.** Nothing here is accepted until the owner approves it at the M1 review gate (#35). Part of the spike [#26](https://github.com/subhadlearner/insightforge-agent/issues/26), implemented by M1.2 ([#30](https://github.com/subhadlearner/insightforge-agent/issues/30)) and M1.3 ([#31](https://github.com/subhadlearner/insightforge-agent/issues/31)). M1 is entirely offline: no model is called, and `insightforge-agent bench` has no command that runs one (`bench baseline` runs only the production deterministic logic).

Code: `src/insightforge_agent/benchmark/`. Cases: `benchmarks/evidence/cases.jsonl` (the 48-case pilot, M1.4 / #32) and the separate few-shot set `benchmarks/evidence/fewshot.jsonl`. Test fixtures live apart, in `tests/fixtures/benchmark/`.

## Judgment types, labels and positive classes

Each case asks one question. A candidate (current code, Haiku, Laya, ...) is a `Judge`: it receives only the case id, type and permitted inputs, and returns a `Verdict`.

| Type | Question | Labels | Positive class |
|---|---|---|---|
| PAIR | Are two statements the same fact? | SAME_FACT, DIFFERENT_FACT, CONTRADICTORY | SAME_FACT (a false positive is a **false merge**) |
| SUPPORT | Does the Passage support the statement? | SUPPORTED, CONTRADICTED, INSUFFICIENT_EVIDENCE | SUPPORTED (**false support**) |
| GROUND | Is the Claim grounded in the cited item? | GROUNDED, NOT_GROUNDED | GROUNDED (an ungrounded Claim passed) |
| INDEPENDENCE | Are two Sources independent? | INDEPENDENT, DEPENDENT, UNDETERMINED | INDEPENDENT (**false independence**) |

Every other label of a type is a negative. The positive class is the costly mistake to avoid, so precision, recall, F1 and the false-positive rate (FPR) are measured against it.

`GROUND` stays binary, but a NOT_GROUNDED case may carry a diagnostic `grounding_failure` (UNSUPPORTED_ENTITY, UNSUPPORTED_NUMBER, UNSUPPORTED_CLAIM, MISSING_CITATION), so the T6 name check and number check can be told apart without widening the label set.

## Inputs by candidate kind

- **PAIR** cases hold two statements and optional Passages, plus optional `structured_a` / `structured_b` (entity, predicate, value, scope, period, entities: the fields T5's merge key is built from). `prediction_view()` defaults to `SEMANTIC` access, which strips the structured fields; only the deterministic baseline asks for `BASELINE` access. A SEMANTIC view cannot be constructed with them.
- **GROUND** cases hold the statement under test, the whole original Passage text, the Passage identity (observation id and index), an optional attribution context (heading, speaker, neighbouring sentence) and a `check`: `QUOTED_SPAN` (an Evidence statement against the span quoted for it; the span is required, and may be nonexistent in a NOT_GROUNDED case) or `CLAIM_PASSAGE` (the T6 Report Claim against a Passage; no span). The two are separate checks and should be reported separately.

## Verdict statuses

- `JUDGED`: the only status that carries a label, which must belong to the case's type.
- `ABSTAIN`: the candidate declined. `ERROR`: the call or parse failed. `NOT_APPLICABLE`: the candidate cannot judge this case type. Each needs a reason. None is ever converted into a label.

Usage (input and output tokens, latency, retries, parse failures) is optional and **unknown unless reported**; unknown is never read as zero. Token counts are totals over all attempts. A candidate that made no call must report explicit zeros. Optional `model_scores` are diagnostics only and never set Evidence Confidence (ADR-0005 rule 4); a Verdict has no confidence field.

## Ground truth and review eligibility

- Ground truth lives only in the case file. Predictions are stored apart (`verdicts.jsonl`, `raw.jsonl`) and joined to cases at report time; no write path goes from predictions to cases.
- Label status: `proposed` (no review data), `reviewed` (reviewer and date; an optional second review must be by a different person and agree), `disputed` (needs a note).
- **Qualification metrics use `reviewed` labels only.** Disputed labels are always excluded. Proposed labels, critical or not, are excluded unless `--allow-proposed` is passed; such a report is stamped `EXPLORATORY`, says it is not a qualification result, and counts the proposed cases it included. When proposed critical cases are excluded the report says how many.
- A second review stores the second reviewer's own label (`second_label`), reviewer and date. A reviewed case needs the second label to equal `expected`; a differing one makes the case `disputed` with both labels kept and a note. The review workflow itself (M1.5) is not part of M1.2.
- Case ids must not contain a label name, so an id cannot leak the answer.

## Denominators

Within a group (case type, type/primary category, or type/expected judgment), over eligible cases:

| Metric | Numerator / denominator | Missing predictions |
|---|---|---|
| Coverage | JUDGED / eligible | reported separately; ABSTAIN, ERROR, NOT_APPLICABLE and not-recorded are counted individually |
| Accuracy | correct label / eligible | wrong |
| Precision | true positives / predicted positives | not predicted positives (inherent to precision, hence coverage is always shown beside it) |
| Recall | true positives / expected positives | a miss |
| FPR | false positives / JUDGED expected negatives | excluded |
| Worst-case FPR | (false positives + missing on negatives) / expected negatives | counted as false positives |
| F1 | 2TP / (2TP + FP + FN) | missing on expected positives are FN |

A zero denominator is reported as **undefined**, never as 0. Intervals are Wilson 95%. Per case type, F1 also has a percentile bootstrap interval. Cohen's kappa (for run-to-run stability and second-review agreement) is undefined, and says why, when there are no pairs or chance agreement is 1.

## Pilot set (M1.4)

48 cases, every label `proposed` and every case `synthetic` (invented companies; nothing is an excerpt, so nothing needs a URL). Nothing here is human-verified until M1.5 reviews it.

| Type | Cases | Categories (count) |
|---|---|---|
| PAIR | 24 | false_merge_negative 6, period_scope_mismatch 4, alias_predicate_synonym 4, paraphrase_qualified_value 3, contradiction 4, temporal_ambiguity 3 |
| SUPPORT | 12 | irrelevant_quotation 3, forecast_vs_historical 3, t6_similarity_numeric 4, positive_control 2 |
| GROUND | 8 | wrong_interpretation 2, misattribution 2, qualifiers_units_abbreviations 3, nonexistent_span 1 |
| INDEPENDENCE | 4 | syndicated 2, genuinely_independent 2 |

**Label conventions** (so a reviewer and a candidate read the same question):
- PAIR `CONTRADICTORY`: same entity, measure, period and scope, but the statements cannot both be true (opposite polarity or direction, different values of a single-valued predicate, a factor of 1,000 apart). `DIFFERENT_FACT`: the statements concern different things (measure, period, scope, entity or relationship), so both can be true. `SAME_FACT`: one fact worded or rounded differently.
- SUPPORT `INSUFFICIENT_EVIDENCE`: the Passage is silent on, or only forecasts or recalls another period of, the statement. `CONTRADICTED`: the Passage states a conflicting value for the same entity, measure and period.
- Relative dates ("last quarter", "this year") are resolved against the Passage's own publication date, which the case supplies.

**Critical** is set for the categories where a wrong positive is the costly error: false_merge_negative, contradiction, irrelevant_quotation, forecast_vs_historical, t6_similarity_numeric and syndicated (22 cases). Every critical case is a negative.

**Look-alike negatives** are tagged: `dollars_vs_vehicles`, `same_name_different_entity`, `same_number_different_period`, `same_number_different_scope`, `million_vs_billion` (a contradiction, since the statements cannot both hold), `rounded_vs_exact` (a SAME_FACT paraphrase, and a SUPPORT case where rounding cannot establish an exact figure) and `syndicated_copy`. All PAIR cases carry structured fields so the deterministic baseline can run on them.

**Few-shot examples** (`fewshot.jsonl`, at most 10) are a separate file in the same schema, built from different companies and wording. A test fails if any example shares an id, input text or company name with a benchmark case; candidates may be shown them, and they are never scored.

## Statistics

Standard library only. Wilson 95% intervals; paired bootstrap F1 intervals (95%, fixed seed, 2,000 resamples; one resample draws case ids once and applies them to every candidate, so pairing is kept; resamples where F1 is undefined are left out and counted); one-sided 95% Clopper-Pearson upper bound on the false-positive rate (`1 - 0.05 ** (1/n)` for zero failures, binomial-tail inversion otherwise); Cohen's kappa.

## Deterministic baseline (M1.3)

`insightforge-agent bench baseline` runs the **real** T5/T6 production functions on each case's BASELINE view, records the Verdicts and prints the report. Nothing is reimplemented; three behaviour-preserving extractions made the needed logic callable, each with golden-value regression tests: `pipeline.synthesize.merge_identity` (the merge key), `pipeline.synthesize.literal_problem` (number and name literal check) and `domain.evidence.sources_independent`. No model, search or fetch can run: those `Deps` slots are tripwires that raise if touched.

Production decides most of these questions with a model (T5 drafts and judges entailment with an LLM). The baseline therefore says **NOT_APPLICABLE** wherever a judgment would need one, rather than guessing, and records what it did compute as diagnostics. Mapping (owner-approved, issue #31):

| Case | Production behaviour used | Label |
|---|---|---|
| PAIR, merge keys equal | `merge_identity` (group key, stated period, canonical value) | SAME_FACT |
| PAIR, keys differ | a nonmerge does not say DIFFERENT_FACT or CONTRADICTORY | NOT_APPLICABLE; the Extraction conflict rule (`detect_conflicts`) is recorded as a diagnostic, never as a label, because production also needs a model CONTRADICTED verdict |
| PAIR, no structured fields | the merge key cannot be formed | NOT_APPLICABLE |
| SUPPORT | `literal_problem` is only a necessary condition; support is a model verdict | always NOT_APPLICABLE; literal result and the real T5 drop outcome are recorded |
| GROUND / CLAIM_PASSAGE | the real `fact_check()` over in-memory fixtures: `HashingEmbedder(256)`, threshold 0.85, numeric check, document path (no fetch) | `verified` -> GROUNDED, `unverified` -> NOT_GROUNDED (failure UNSUPPORTED_CLAIM below the threshold, else UNSUPPORTED_NUMBER); a historical Claim is `unchecked` -> NOT_APPLICABLE |
| GROUND / QUOTED_SPAN | `literal_problem` of the statement against the quoted span | fail -> NOT_GROUNDED (UNSUPPORTED_NUMBER or UNSUPPORTED_ENTITY); pass -> NOT_APPLICABLE (T5 has no span-support judgment) |
| INDEPENDENCE | `sources_independent` (different domains, neither text links to the other) | True -> INDEPENDENT, False -> DEPENDENT (never UNDETERMINED) |

`fact_check` verifies by embedding similarity plus a numeric check. It is **production verification, not semantic entailment**, and every CLAIM_PASSAGE Verdict says `semantic_entailment=false`. `INDEPENDENT` means only that production's lexical predicate holds (syndication and common ownership are invisible to it). The baseline reports explicit zero tokens (it made no call) and leaves latency unknown. Scripted model replies may exercise production orchestration in tests but are never recorded as predictions.

### Evidence dispositions

A Verdict may carry `evidence_disposition` (and `evidence_reason`) diagnostics, which reports count: `MERGED` / `NOT_MERGED` (PAIR), `DROPPED_LITERAL_CHECK` (no Passage passes the literal-value check, with the reason: value or name not in Passage) and `PASSES_LITERAL_CHECK_ENTAILMENT_NOT_RUN` (a model would decide the rest). A candidate that reports none leaves them out. **Budget-cut Evidence cannot be shown**: a case holds one item, not a ranked bundle, so the report lists it as not evaluated.

## Reporting additions (M1.3)

- **Input view is strict.** `build_report(..., access=)` and `bench report --input-access` take the view the candidate was meant to see (`SEMANTIC` default, `BASELINE` for the baseline). A recording made on the other view is stale (excluded, counted as missing); the two hashes are never accepted interchangeably.
- **False contradiction** (PAIR: predicted CONTRADICTORY, SUPPORT: predicted CONTRADICTED, where the expected label differs) is reported on its own, over judged cases not expected to be a contradiction. It is separate from false merge and false support.
- **GROUND subtypes** `QUOTED_SPAN` and `CLAIM_PASSAGE` have their own groups; the combined GROUND figure carries a notice not to read it as one thing.
- **Accounting.** Coverage, abstain, error, NOT_APPLICABLE, not-recorded and stale are counted separately, and each unjudged status is counted by its reason.
- **Not evaluated, stated in every report:** false-HIGH Confidence (it needs source credibility and the full Confidence rule, which cases do not carry; INDEPENDENCE measures false independence only) and budget-cut Evidence.

## CI tiers

| Tier | Marker | What it proves | Network |
|---|---|---|---|
| 1 | `baseline` | the real deterministic production logic on cases | blocked |
| 2 | `replay` | recorded predictions, replay, stale recordings, reports | blocked |
| 3 | `scripted` | parsing, invalid output, retries and failure handling, using `tests/scripted.py` | blocked |
| 4 | `live` | a real model or server; needs `-m live` and `INSIGHTFORGE_ALLOW_LIVE_TESTS=1` | allowed |

`pytest` excludes `live` through `addopts` (`-m 'not live'`), and CI runs `uv run pytest -m baseline`, `-m replay`, `-m scripted`, then the rest (`.github/workflows/ci.yml`). Tiers 1-3 (every non-`live` test) fail on a connection to a non-loopback address or a DNS lookup of a non-loopback name; the attempt is remembered, so code that swallows the error still fails the test at teardown. **Live tests need two opt-ins: `-m live` and `INSIGHTFORGE_ALLOW_LIVE_TESTS=1`.** Without the variable they are skipped before any fixture runs, so no provider is reached; they use your real environment and `.env` and make paid calls. A recording can never replace a reviewed label (predictions and cases are stored apart), and a live recording needs review before becoming a fixture.

## Limitations

- **Zero observed failures is not proof of safety.** With `n` judged negatives and none failed, the rate may still be as high as the one-sided upper bound (about 5% at n = 59). Small benchmarks give wide intervals; the pilot is indicative, not a qualification of any candidate.
- Categories are small; per-category figures are descriptive. Many groups are compared, and no multiple-comparison correction is applied.
- Synthetic cases may not resemble real Passages. Reviewers are few; kappa measures agreement, not correctness.
- Bootstrap intervals assume cases are exchangeable; they are not adjusted for correlated cases from one Source.
- Abstention can raise precision and lower FPR; read them with coverage and the worst-case FPR.
- The baseline is a floor, not a verdict on semantics: lexical and structural checks only. Its high NOT_APPLICABLE share is the honest result; coverage is never inflated by guessing.
- A PAIR case's merge-key outcome depends on the structured fields the case supplies, which stand in for T5's drafting model.
- CLAIM_PASSAGE uses a hashing embedder (bag of words): near-paraphrases score low, and near-copies with one changed word score high.
- Recorded runs are replayed for reporting. A recording is bound to the exact prediction-facing case by a content hash.

## Code boundary (import-linter)

- **Production never imports the benchmark** (domain, pipeline, agents, stores, api, scheduler, config, llm, embeddings, demo).
- **Benchmark to production is allowed for the logic a baseline must run**, so M1.3 adapters call the real T5 and T6 functions rather than reimplementing them: `domain.*` (including `evidence`, read only), `pipeline.synthesize`, `extract`, `fact_check`, `write`, `deps`, `citations`, `stores.source_store`, `stores.memory`, `embeddings`. Models are injected, so offline runs use scripted fakes.
- **Banned**: `llm` and `pipeline.wiring` / `pipeline.graph` even through import chains (they build real models or run the whole graph); and direct imports of `agents`, `config`, `api`, `scheduler`, `stores.sqlite`, `pipeline.dispatch`, `demo`. `config` cannot be banned through chains because `pipeline.deps` reaches it via `agents.web` (settings only; no model or network call at import); removing that would refactor production code and belongs, if wanted, to M1.3.
- Evidence Confidence stays out of reach of model scores because a `Verdict` has no confidence field (tested), not because `domain.evidence` is unimportable.
- Paid or network use also needs a client library; none is a dependency, and a test checks that no benchmark module imports one.

## Recording and replay

A record is keyed by `(candidate, case_id, run)` and carries the candidate version, model, a safe configuration (an explicit allow-list of fields; anything else is dropped and its name noted), configuration and case hashes. Re-recording identical content is a no-op; any difference (including a different candidate version) fails. A recording is **stale** when its case hash, candidate version, model or configuration hash no longer matches: resume refuses it (use a new run id) and replay refuses it; a report excludes it and counts it as missing. A run holding more than one candidate version, model or configuration is never aggregated: the report errors unless `--candidate-version` selects one. Secrets are redacted before anything is written.

## Cost controls (offline primitives)

- Money is `Decimal`. Before a job, the conservative high estimate (sum of per-call worst cases) must fit the remaining budget, or nothing runs. A call without provable input and output token ceilings is rejected.
- The worst case is reserved before each call, sequentially (one open reservation at a time), then reconciled to actual usage when both token counts are known. Unknown usage, a failure or a provider error keeps the worst case as spent.
- Spend and reservations live in an append-only, hash-chained, fsynced ledger that is cumulative across runs, restarts and execution ids. A corrupt chain or an unsettled reservation (interrupted run) fails closed; recovery is an explicit owner action that keeps the worst case as spent.
- A ledger write failure poisons the ledger (every later operation refuses); a failed reservation write means no call is made. If actual cost exceeds the reserved worst case, execution stops even when overall budget remains, since the bound was wrong.
- Partial results are kept; a restarted run skips recorded cases, including a case recorded as `ERROR`. Retrying a failed case needs a new run id and is paid for again.
- The ledger assumes one process at a time (there is no file lock) and detects tampering inside the chain, not deletion of its tail. The owner reconciles it against provider billing and git history before any live run (#26 M2 plan).
- **Authorisation is not granted here.** A `--approved-budget` number is necessary but never sufficient: `BudgetGuard` refuses an unverified `Authorization`. M2 must verify the owner's approval of the manifest before constructing a verified one. No paid provider execution exists in M1.
