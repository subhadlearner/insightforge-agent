# F2: Fact-Checker accuracy

Follows audit finding F2 (`2026-10-11-evidence-quality-audit.md`). Policy under test (ledger row 13, T6): cosine similarity >= 0.85 between the Claim and the best re-fetched Passage, every figure of the Claim stated in that Passage (`unsupported_numbers`), otherwise `unverified`.

## Post-T7 result: production `bge-small` (2026-10-11)

**Outcome: the semantic embedder does not satisfy F2 on its own. Owner decision required; no production code changed.**

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

### Owner decisions

- **(a) Entailment step.** After the similarity gate and numeric check, a light-model call ("does this Passage state this Claim?") on the matched text, reusing T5's `ENTAIL_SYSTEM` / `ask_json` path. Changes ledger row 13 and T6's "no model is called", and adds one paid light call per sampled Claim (about 6-8 per Report). Architecturally acceptable, not implemented.
- **(b) Per-sentence matching** of the Claim within each re-fetched Passage, threshold kept at 0.85. Fixes the paragraph cases (verbatim reaches 1.000 per sentence). Only with (a): alone it verifies the wrong-measure and wrong-entity Claims everywhere.
- **(c) Accept current behaviour** and record the known limits: true Claims in long paragraphs are under-verified (lowers the pass rate), and wrong-measure or wrong-entity Claims against short Passages are over-verified.

## Tests

`tests/integration/test_f2_fact_check_bge.py`, in the `integration` tier (real model from the local cache; run `uv run insightforge-agent models download` once). That tier is excluded from the default run and from CI, like the other real-model tests. Every case prints its verdict, similarity, threshold, reason, Claim and Passage: `-rP` shows them for passing cases, `--runxfail` for the known failures. Every bge-small figure in the table above comes from those runs.

- Pass: 5 correct Claims verified, 4 unsupported Claims rejected.
- `xfail(strict=True, raises=AssertionError)`: 3 correct Claims in a paragraph, 3 wrong-measure or wrong-entity Claims. A fix that makes one pass fails the suite until its marker is removed. Setup problems raise `RuntimeError`, so they cannot be mistaken for the expected failure.

Coverage notes:
- **CI.** CI does not run this file. The numeric check is still covered there with the hashing embedder (`test_numeric_claim_needs_its_number_in_the_matched_passage`).
- **Margin.** The wrong-year-in-paragraph guard passes at 0.834, 0.016 under the threshold.
- **Model name.** The `embedder` fixture (`tests/integration/conftest.py`) names `BAAI/bge-small-en-v1.5` directly. That equals `Settings.embedding_model` today, but this file would not follow a change of configured model.

The pre-T7 file `tests/pipeline/test_f2_fact_check_regression.py` (branch `f2-fact-checker-investigation`, `ab35d25`) is not carried forward: it ran on `HashingEmbedder`, the `Deps` default for tests, which is no longer the production embedder. Its cases are all in the new file. The benchmark baseline (`src/insightforge_agent/benchmark/baseline.py`) still pins `HashingEmbedder(256)` by design (M1.3), so its CLAIM_PASSAGE scores do not reflect production; its comment calling that "the production default" is now out of date (not changed here).

## Pre-T7 investigation (hashing embedder, `ab35d25`)

The first investigation ran before the semantic embedder was wired, with `HashingEmbedder(256)`, and stopped under the F2 rule. Its findings:

- Paragraph-level hashing similarity failed paraphrases and abbreviations (0.50-0.66).
- Per-sentence or per-clause matching with hashing fixed word-for-word Claims but verified wrong-measure and wrong-entity Claims at 0.952, because a bag of words scores a one-word change highly. A simulated sentence-level `fact_check` broke 2 of 3 guards.
- It predicted that a semantic embedder would fix paraphrases but would likely not separate "R&D spend $90M" from "revenue $90M", and that a judgment (entailment) would be needed. The post-T7 measurements above confirm both predictions.
