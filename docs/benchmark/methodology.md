# Benchmark methodology (PROPOSED)

**Status: PROPOSED.** Nothing here is accepted until the owner approves it at the M1 review gate (#35). Part of the spike [#26](https://github.com/subhadlearner/insightforge-agent/issues/26), implemented by M1.2 ([#30](https://github.com/subhadlearner/insightforge-agent/issues/30)). M1 is entirely offline: no model is called, and `insightforge-agent bench` has no command that runs one.

Code: `src/insightforge_agent/benchmark/`. Cases: `benchmarks/evidence/cases.jsonl` (written by #32; not part of M1.2). Test fixtures live apart, in `tests/fixtures/benchmark/`.

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

## Verdict statuses

- `JUDGED`: the only status that carries a label, which must belong to the case's type.
- `ABSTAIN`: the candidate declined. `ERROR`: the call or parse failed. `NOT_APPLICABLE`: the candidate cannot judge this case type. Each needs a reason. None is ever converted into a label.

Usage (input and output tokens, latency, retries, parse failures) is optional and **unknown unless reported**; unknown is never read as zero. Token counts are totals over all attempts. A candidate that made no call must report explicit zeros. Optional `model_scores` are diagnostics only and never set Evidence Confidence (ADR-0005 rule 4); a Verdict has no confidence field.

## Ground truth and review eligibility

- Ground truth lives only in the case file. Predictions are stored apart (`verdicts.jsonl`, `raw.jsonl`) and joined to cases at report time; no write path goes from predictions to cases.
- Label status: `proposed` (no review data), `reviewed` (reviewer and date; an optional second review must be by a different person and agree), `disputed` (needs a note).
- **Qualification metrics use `reviewed` labels only.** Disputed labels are always excluded. Proposed labels, critical or not, are excluded unless `--allow-proposed` is passed; such a report is stamped `EXPLORATORY`, says it is not a qualification result, and counts the proposed cases it included. When proposed critical cases are excluded the report says how many.
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

## Statistics

Standard library only. Wilson 95% intervals; paired bootstrap F1 intervals (95%, fixed seed, 2,000 resamples; one resample draws case ids once and applies them to every candidate, so pairing is kept; resamples where F1 is undefined are left out and counted); one-sided 95% Clopper-Pearson upper bound on the false-positive rate (`1 - 0.05 ** (1/n)` for zero failures, binomial-tail inversion otherwise); Cohen's kappa.

## Limitations

- **Zero observed failures is not proof of safety.** With `n` judged negatives and none failed, the rate may still be as high as the one-sided upper bound (about 5% at n = 59). Small benchmarks give wide intervals; the pilot is indicative, not a qualification of any candidate.
- Categories are small; per-category figures are descriptive. Many groups are compared, and no multiple-comparison correction is applied.
- Synthetic cases may not resemble real Passages. Reviewers are few; kappa measures agreement, not correctness.
- Bootstrap intervals assume cases are exchangeable; they are not adjusted for correlated cases from one Source.
- Abstention can raise precision and lower FPR; read them with coverage and the worst-case FPR.
- Recorded runs are replayed for reporting. A recording is bound to the exact prediction-facing case by a content hash.

## Recording and replay

A record is keyed by `(candidate, case_id, run)` and carries the candidate version, model, a safe configuration (an explicit allow-list of fields; anything else is dropped and its name noted), configuration and case hashes. Re-recording identical content is a no-op; any difference (including a different candidate version) fails. Secrets are redacted before anything is written.

## Cost controls (offline primitives)

- Money is `Decimal`. Before a job, the conservative high estimate (sum of per-call worst cases) must fit the remaining budget, or nothing runs. A call without provable input and output token ceilings is rejected.
- The worst case is reserved before each call, sequentially (one open reservation at a time), then reconciled to actual usage when both token counts are known. Unknown usage, a failure or a provider error keeps the worst case as spent.
- Spend and reservations live in an append-only, hash-chained, fsynced ledger that is cumulative across runs, restarts and execution ids. A corrupt chain or an unsettled reservation (interrupted run) fails closed; recovery is an explicit owner action that keeps the worst case as spent.
- Partial results are kept; a restarted run skips recorded cases.
- **Authorisation is not granted here.** A `--approved-budget` number is necessary but never sufficient: `BudgetGuard` refuses an unverified `Authorization`. M2 must verify the owner's approval of the manifest before constructing a verified one. No paid provider execution exists in M1.
