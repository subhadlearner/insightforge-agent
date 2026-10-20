# T18 B-1: gate exception and known limitation

For the T18 final verification evidence (Refs #19). This was decided by the owner on GPT-6's review of the historical Claim audit on 2026-10-21. Nothing in this note changes the fact-checker, model, prompts, benchmark or run records.

## Status

**B-1 (entailment calibration) is formally FAILED and stays failed.** It will not be re-run for the submission. T18 proceeds under this documented exception.

| Run | Code | Calls | Est. cost | Result | Record SHA-256 |
|---|---|---|---|---|---|
| 1 | `e517b28` | 74 | $0.0129 | FAIL: N25, N40 SUPPORTED; Historical 2.0, 3.1 not judged (harness kept the report date) | `2157c0eb18044bb7168d6aa9d98290030da5cb092532b710b7ee9ccd992704ad` |
| 2 | `7304544` | 76 | $0.0156 | FAIL: verified 2.6, 3.3, Historical 3.1 lost; N28 unreadable reply | `848b4f686399e3b41758f541ecad4c210d08b1014a38e6eea2f62f88755d4a3b` |
| 3 | `bac1a11` | 76 | $0.0152 | FAIL: verified `run_ab1c836b61f4 1.5` judged INSUFFICIENT_EVIDENCE | `7b2bd1d8fdaadcefd8a4dd231d6b5f82e009e22fff3e2c27ad1e267aa278cfd3` |

The costs are the harness's estimates from provider-reported tokens, not billed amounts.

Run 3 otherwise met every rule:
- 0 of 40 adversarial negatives SUPPORTED;
- all 10 positives SUPPORTED, including the 8 gated ones;
- 25 of 26 verified Claims SUPPORTED, and 3.3 validly INSUFFICIENT_EVIDENCE as adjudicated;
- no unreadable reply.

## Why the failure is excepted, not fixed

An offline audit labelled all 27 historical verified Claims against the single-Passage F2 contract ([audit report](2026-10-21-t18-b1-historical-claim-audit.md)). The report has two SHA-256 hashes, one per line-ending form:
- `3f8f100c8d57e5c9c4f72771e6183f4d6c24e0e3f310e8f5df1bb4eaf8f52da3`: the reviewed copy, with Windows (CRLF) line endings, as a Windows checkout restores it;
- `de054c640ecc54017d215cb41d6a8a9e26a9f87aff7f7253539eb4df1d0d03b8`: LF line endings, as git stores it. It found that 24 are supported and 3 are not (`1.5`, `2.9`, `3.3`). Each of the 3 carries a qualifier that appears only outside its cited Passage.

- **`1.5` was correctly rejected.** Its historical VERIFIED label is defective: the Passage gives Microsoft's 20% share but never says it is of cloud infrastructure services. B-1 failed for asking the verifier to reproduce an incorrect label.
- **`3.3` was correctly rejected.** Its label was already adjudicated INSUFFICIENT_EVIDENCE ("solid-state" is only in the Source title).
- **`2.9` is a genuine false SUPPORTED.** The Passage never says Alphabet's $71-73 billion is capex (that word is only in a section heading), yet all three runs verified it. The gate scored it as a pass only because the defective historical label agreed.

## Known limitation

The F2 judge can verify a Claim whose market definition, financial metric or other qualifier is stated only elsewhere on the same page, not in the cited Passage.
- **Frequency:** under the current instruction it accepted such a qualifier in 3 of 6 judgments (B-1 runs 2 and 3).
- **Impact:** the leaked Claims seen so far are true according to their own Source, but not according to the cited Passage alone. A verified Claim's qualifier may therefore not be traceable to its citation.
- **Coverage:** the B-1 negatives do not test this class.
- **Follow-up:** this is tracked post-submission in #75. It is not addressed for the submission.

## Limits of this evidence

- One sample per case per run.
- The audit's labels are one auditor's provisional findings, accepted by GPT-6. `2.9` is a moderate-confidence call.
- The positives and negatives were not re-audited.
- The harness does not save request text, so run 3's requests were rebuilt offline from the same code and matched to the 76 logged calls.
