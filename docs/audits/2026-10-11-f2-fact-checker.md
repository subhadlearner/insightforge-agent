# F2: Fact-Checker accuracy

Follows audit finding F2 (`2026-10-11-evidence-quality-audit.md`). The original policy (ledger row 13, T6): cosine similarity >= 0.85 between the Claim and the best re-fetched Passage, every figure of the Claim stated in that Passage (`unsupported_numbers`), otherwise `unverified`.

## Status: implemented, pending one real-model validation (2026-10-12)

**The owner approved a change to the verification policy, and it is implemented. F2 is not closed:** the entailment step is tested only with scripted judges. The owner will decide separately whether to authorise one small real light-model validation.

**Why the policy changed instead of the threshold.** Measured with the production `bge-small` embedder (section below), no similarity threshold separates correct Claims from wrong ones:

| Claim against "BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024." (or its own Passage) | bge-small | Should be |
|---|---|---|
| Correct Writer paraphrase | 0.961 | verified |
| Wrong measure ("exported" for "sold") | **0.963** | unverified |
| g-ma-02, R&D spend $90M against "revenue $90M; R&D $40M" | **0.948** | unverified |

The owner therefore approved, on 2026-10-12, both options from the decision brief below: (b) matching per sentence and (a) an entailment judgment after the deterministic gates. Similarity is now a relevance filter, not the final criterion.

**Approved pipeline** (`pipeline/fact_check.py`, design.md section 7 and F2 notes, ledger row 13):
1. **Spans.** The same Passages as before (re-fetched web, or stored document chunks), cut into every sentence plus every two-sentence window (`domain/passages.candidate_spans`). Each span keeps its parent Passage, which stays the citation.
   - One extension beyond the brief: for a Claim of more than two sentences, windows are as wide as the Claim, up to four sentences (`window_width`).
   - Why: with two-sentence windows only, a three-sentence Claim can never find all its figures in one span. The T7 real-PDF integration test showed this; its Claims are whole chunks, and under the old whole-Passage rule they verified.
   - Single-sentence Claims, the normal case, are unaffected.
2. **Relevance.** The production embedder (bge-small) scores the Claim against every span. Spans under 0.85 are dropped (threshold unchanged).
3. **Figures.** Spans that do not state every figure or year of the Claim are dropped (numeric check unchanged). With no span left: `unverified`, no model call.
4. **Support.** The best 3 spans go to **one** light-model entailment call. Overlapping spans share one place, widened to the largest of them that passes the gates, so a sentence keeps its subject. Each judged span carries the similarity computed for its own text. The 3 best by that score are judged, and that score is the one a verdict reports. The call uses Synthesis's prompt and schema (`pipeline/entailment.py`). The whole request is held within `passage_token_cap` by dropping the weakest spans; if even the best one alone does not fit, no call is made and the Claim is `unverified`. The Claim is `verified` only if a span is judged SUPPORTED, unambiguously.
5. **Fail closed.** Any ordinary exception from the model path (`AssertionError` included), invalid JSON, or no usable verdict leaves the Claim `unverified` and logs `fact_check_entailment_failed`. Only `KeyboardInterrupt` and `SystemExit` propagate. It is never `unchecked`, which the pass rate would leave out.

Unchanged: bge-small, 0.85, the numeric check, sampling (20%, minimum 5), `[UNVERIFIED]`, and the re-fetch versus `document_passages` split. Not used: no entity or measure lexicon, no regex reading of meaning, no other embedding model, no new resolver. Sentence splitting is orthographic: a stop before an upper-case letter. There is no abbreviation list.

**Cost.** At most one light call per sampled Claim, none when the gates leave no span: about 5-8 calls for a typical Report.

### Final regression matrix (real bge-small retrieval, scripted oracle judge)

`tests/integration/test_f2_fact_check_bge.py`, offline, `-m integration`. Retrieval, gates and plumbing are real; the judge is an oracle that says SUPPORTED only for a span containing the sentence that states the Claim. So a correct Claim verifies only if bge-small retrieved that sentence, and a wrong one is rejected because the judge rejects it. **Whether a real light model judges as the oracle does is the open validation.**

| Case | Judged span (its own score) | Gates | Model call | Verdict | Before F2 |
|---|---|---|---|---|---|
| g-qu-02, gigawatt-hours vs GWh | 0.871 | pass | 1 | verified | verified |
| Writer paraphrase, one sentence | 0.961 | pass | 1 | verified | verified |
| General Motors vs GM / GM vs General Motors | 0.987 / 0.987 | pass | 1 | verified | verified |
| BYD vs BYD Co. Ltd. | 0.990 | pass | 1 | verified | verified |
| Word-for-word Claim, 3-sentence paragraph | 0.891 (sentence 1.000, widened to its 2-sentence window) | pass | 1 | **verified** | unverified (0.842) |
| Writer paraphrase, same paragraph | 0.864 (sentence 0.961, widened) | pass | 1 | **verified** | unverified (0.814) |
| Light paraphrase, same paragraph | 0.868 (sentence 0.971, widened) | pass | 1 | **verified** | unverified (0.828) |
| Subject in the previous sentence ("BYD is a Chinese automaker. It sold 3.02 million vehicles in 2023.") | 0.877 (the window naming BYD) | pass | 1 | verified | (added after review) |
| Wrong year, one sentence | 0.987 | numeric gate ("2023") | 0 | unverified | unverified |
| Wrong year, paragraph | 0.987 | numeric gate ("2023") | 0 | unverified | unverified (0.834) |
| g-ma-02, wrong measure, right number | 0.948 | pass | 1 | **unverified** (judge) | verified |
| Wrong measure ("exported"), one sentence | 0.963 | pass | 1 | **unverified** (judge) | verified |
| Wrong measure, paragraph | 0.963 | pass | 1 | unverified (judge) | unverified (0.800, dilution only) |
| Wrong entity ("Tesla"), one sentence | 0.857 | pass | 1 | **unverified** (judge) | verified |
| Wrong entity, paragraph | 0.857 | pass | 1 | unverified (judge) | unverified (0.746, dilution only) |

No xfails remain. The "model call" column is pinned by the tests: bge-small passes every wrong-measure and wrong-entity Claim to the judge, so their rejection rests entirely on the entailment call. The numeric gate alone stops wrong years.

### Remaining limitations

- **Real-model behaviour is unmeasured.** Every rejection of a wrong-measure or wrong-entity Claim depends on the light model answering as the oracle does. One authorised live validation is the next step.
- **Top-3 cut.** If more than 3 groups of overlapping spans pass the gates, only the 3 most similar are judged, so a supporting span ranked fourth is not seen.
- **Sentence splitting is orthographic.** An abbreviation before a capital ("Co. Ltd.") cuts a sentence, and only a window puts it back together. A stop before a digit ("...in 2024. 2025 was...") does not split, so that span is longer. A Claim of more than four sentences is matched only through four-sentence windows, so it may fail the numeric gate (fail closed).
- **Token cap.** The entailment call counts against `run_token_cap`. When a cap is configured (none by default), exceeding it during fact-checking fails the Run, as it does in Synthesis, under the existing Run-limit policy.
- **The benchmark baseline cannot verify.** It makes no model call, so a CLAIM_PASSAGE case that passes the gates is now NOT_APPLICABLE (baseline `m1.3-2`); it still uses `HashingEmbedder(256)`, not bge-small.

## Post-T7 measurement before the fix: production `bge-small` (2026-10-11)

**Outcome then: the semantic embedder did not satisfy F2 on its own. This section is the evidence for the owner's decision; the "Verdict now" column describes the old policy.**

Measured offline through the real `fact_check()` with `FastEmbedEmbedder("BAAI/bge-small-en-v1.5")` from the local cache, threshold 0.85, numeric check unchanged (`tests/integration/test_f2_fact_check_bge.py`, `uv run pytest -m integration`). Each page is one paragraph, so one Passage. Hashing figures are from the pre-T7 investigation below.

| Case | Should be | Hashing (pre-T7) | **bge-small** | Verdict now | Test |
|---|---|---|---|---|---|
| g-qu-02, "4.27 gigawatt-hours" vs "4.27 GWh" | verified | 0.639 u | **0.871** | verified | pass |
| Writer paraphrase, one-sentence Passage | verified | - | **0.961** | verified | pass (new) |
| Entity name: "General Motors" vs "GM" (and the reverse) | verified | - | **0.987** / **0.987** | verified | pass (new) |
| Entity name: "BYD" vs "BYD Co. Ltd." | verified | - | **0.990** | verified | pass (new) |
| Claim copied word for word, 3-sentence paragraph | verified | 0.659 u | **0.842** | unverified | xfail |
| Writer paraphrase, same paragraph | verified | 0.585 u | **0.814** | unverified | xfail |
| Light paraphrase ("BYD sold 4.27 million vehicles in 2025."), same paragraph | verified | 0.496 u | **0.828** | unverified | xfail |
| g-ma-02, "R&D spend was $90M" vs "revenue $90M; R&D $40M" | unverified | 0.864 V | **0.948** | **verified** | xfail |
| Wrong measure ("exported" for "sold"), one-sentence Passage | unverified | (0.952 V) | **0.963** | **verified** | xfail (new) |
| Wrong entity ("Tesla" for "BYD"), one-sentence Passage | unverified | (0.952 V) | **0.857** | **verified** | xfail (new) |
| Wrong measure, 3-sentence paragraph | unverified | 0.659 u | 0.800 | unverified | pass |
| Wrong entity, 3-sentence paragraph | unverified | 0.635 u | 0.746 | unverified | pass |
| Wrong year, 3-sentence paragraph | unverified | 0.635 u | 0.834 | unverified (below 0.85) | pass |
| Wrong year, one-sentence Passage | unverified | (0.952) | 0.987 | unverified (numeric: 2023) | pass (new) |

Hashing figures in brackets were measured per sentence in the pre-T7 probe, which is the same text as a one-sentence Passage.

### What this shows

1. **Paraphrase, unit abbreviation and entity-name variation: fixed where the Passage is about one sentence.** g-qu-02, the Writer paraphrase and the entity-name variations now verify. The strict xfail for g-qu-02 is now an ordinary passing test.
2. **Correct Claims inside a longer paragraph still fail**, at 0.81-0.84. A one-sentence Claim against a three-sentence Passage is diluted even with a semantic embedder. Not fixable without tuning the threshold (ruled out) or matching per sentence (a change to how criterion 13 is applied).
3. **Wrong-measure and wrong-entity Claims are verified** whenever the Passage is short: g-ma-02 (0.948, worse than hashing's 0.864), "exported" for "sold" (0.963) and "Tesla" for "BYD" (0.857). The same edits are rejected inside the three-sentence paragraph only because of the dilution in point 2, not because anything recognises them. Sentence-level matching would therefore expose these Claims everywhere.
4. **Numeric and year safeguards hold.** Wrong years are rejected by the numeric check where similarity passes (0.987, "2023" missing), and by similarity where it does not.

### Failing unsupported Claims (decision brief)

| | g-ma-02 | Wrong measure | Wrong entity |
|---|---|---|---|
| Claim | Orrin Bio's R&D spend was $90 million. | BYD exported 4.27 million vehicles in 2025, up from 3.02 million in 2024. | Tesla sold 4.27 million vehicles in 2025, up from 3.02 million in 2024. |
| Supporting Passage | Orrin Bio's revenue was $90 million; its R&D spend was $40 million. | BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024. | (same) |
| bge-small similarity | 0.948 | 0.963 | 0.857 |
| Current / desired | verified / unverified | verified / unverified | verified / unverified |
| Numeric check | Passes: $90M is in the Passage, bound to a different measure. The check tests presence, not binding. | Passes: every figure is present. | Passes: every figure is present. |
| Deterministic rule? | Not safely. Binding a figure to its measure ("$90M" -> revenue) needs phrase parsing that breaks on ordinary rewording ("revenue rose to $90M", "$90M in revenue"). Splitting on `;` fixes this case only by punctuation. | Not safely. Telling "exported" from "sold" but "sold" from "sales reached" or "delivered" needs a measure lexicon; it would reject valid paraphrases. | Mostly already in place upstream. The Writer (`pipeline/write.py`, `_opening_names` and `named_entities_in`) already rejects a Claim naming a capitalised word, sentence-initial included, that the cited Evidence item does not contain (`test_writer_rejects_a_sentence_initial_name_the_evidence_does_not_contain` uses this exact "Tesla" Claim). So a Writer-produced wrong-entity Claim fails before fact-checking unless the Evidence item itself is wrong. The same rule at the Fact-Checker (names checked against the matched Passage) would be deterministic, but it would reject "GM" against "General Motors", which similarity verifies (0.987), and it adds a criterion to row 13. |

**Does a light-model entailment check appear necessary? Yes**, for wrong-measure Claims. No similarity score separates them: the wrong Claims score as high as or higher than correct ones (0.948-0.963 against 0.961 for the paraphrase). The Writer has no measure check, so nothing upstream catches them either. For wrong entities the need is weaker because of the Writer rule above. An entailment judgment on the matched Passage (or sentence), after the 0.85 gate, would reject all three. On its own it does not fix point 2: Claims under 0.85 never reach it, so per-sentence matching would still be needed for Claims inside longer paragraphs, and per-sentence matching is only safe with the entailment step behind it.

### Owner decisions (options as presented; (a) and (b) approved 2026-10-12)

- **(a) Entailment step.** After the similarity gate and numeric check, a light-model call ("does this Passage state this Claim?") on the matched text, reusing T5's `ENTAIL_SYSTEM` / `ask_json` path. Changes ledger row 13 and T6's "no model is called", and adds one paid light call per sampled Claim (about 5-8 per Report). **Approved and implemented.**
- **(b) Per-sentence matching** of the Claim within each re-fetched Passage, threshold kept at 0.85. Fixes the paragraph cases (verbatim reaches 1.000 per sentence). Only with (a): alone it verifies the wrong-measure and wrong-entity Claims everywhere. **Approved and implemented** (sentences plus two-sentence windows).
- **(c) Accept current behaviour.** Not chosen.

## Tests

- `tests/domain/test_spans.py`: sentence splitting and spans are deterministic and bounded. That is 2n - 1 spans per Passage for a short Claim, windows at most 4 sentences, and each sentence at most 600 characters.
- `tests/pipeline/test_f2_fact_check_entailment.py` (default run and CI; scripted judge, hashing embedder):
  - no model call when no span clears 0.85, or when the numeric gate rejects every candidate;
  - one call, at most 3 spans, judges every candidate;
  - any SUPPORTED span verifies, and the match is its parent Passage;
  - CONTRADICTED only, INSUFFICIENT_EVIDENCE only, a malformed reply, a model exception, or omitted, out-of-range or ambiguous verdicts all give `unverified`, never `unchecked`.
- `tests/integration/test_f2_fact_check_bge.py` (`-m integration`, real bge-small from the local cache; run `uv run insightforge-agent models download` once). It has the 16 F2 cases in the matrix above (15 from the brief, plus a subject-in-the-previous-sentence case added after review). `-rP` prints each case's spans, scores, model calls and verdict. A setup problem raises `RuntimeError`, never a verdict assertion.
- **CI** does not run the integration tier. The gates and the entailment logic are covered there with the hashing embedder and scripted judges.
- **Model name.** The `embedder` fixture (`tests/integration/conftest.py`) names `BAAI/bge-small-en-v1.5` directly. That equals `Settings.embedding_model` today, but this file would not follow a change of configured model.

The pre-T7 file `tests/pipeline/test_f2_fact_check_regression.py` (branch `f2-fact-checker-investigation`, `ab35d25`) is not carried forward: it ran on `HashingEmbedder`, which is no longer the production embedder. Its cases are all in the integration file.

## Pre-T7 investigation (hashing embedder, `ab35d25`)

The first investigation ran before the semantic embedder was wired, with `HashingEmbedder(256)`, and stopped under the F2 rule. Its findings:

- Paragraph-level hashing similarity failed paraphrases and abbreviations (0.50-0.66).
- Per-sentence or per-clause matching with hashing fixed word-for-word Claims but verified wrong-measure and wrong-entity Claims at 0.952, because a bag of words scores a one-word change highly. A simulated sentence-level `fact_check` broke 2 of 3 guards.
- It predicted that a semantic embedder would fix paraphrases but would likely not separate "R&D spend $90M" from "revenue $90M", and that a judgment (entailment) would be needed. The post-T7 measurements above confirm both predictions.
