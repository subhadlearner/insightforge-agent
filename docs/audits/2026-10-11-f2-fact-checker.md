# F2: Fact-Checker accuracy, investigation result (2026-10-11)

**Outcome: not fixed. Stopped under the F2 rule:** "If F2 cannot be fixed adequately before T7, document the dependency and stop." No production code was changed. This branch adds only regression tests and this note. Follows audit finding F2 (`2026-10-11-evidence-quality-audit.md`).

## What was tried

The probe was offline and used the production `HashingEmbedder(256)`, `cosine`, `unsupported_numbers` and the approved 0.85 threshold. It compares the Claim with the whole Passage (today), with each sentence, and with each clause (`;`). V = verified, u = unverified.

| Case | Should be | Paragraph (today) | Sentence | Clause |
|---|---|---|---|---|
| g-qu-02, "4.27 gigawatt-hours" vs "4.27 GWh" | V | 0.639 u | 0.639 u | 0.639 u |
| g-ma-02, R&D $90M vs "revenue $90M; R&D $40M" | u | **0.864 V** | **0.864 V** | 0.756 u |
| Claim copied word for word, 3-sentence paragraph | V | 0.659 u | 1.000 V | 1.000 V |
| Writer paraphrase, same paragraph | V | 0.585 u | 0.819 u | 0.819 u |
| Light paraphrase ("BYD sold 4.27 million vehicles in 2025.") | V | 0.496 u | 0.772 u | 0.772 u |
| Wrong measure ("exported" for "sold") | u | 0.659 u | **0.952 V** | **0.952 V** |
| Wrong entity ("Tesla" for "BYD") | u | 0.635 u | **0.952 V** | **0.952 V** |
| Wrong year | u | 0.635 u | 0.952 u (numbers) | 0.952 u |

## Why sentence-level matching is not the fix

- **It accepts unsupported Claims.** A bag of words scores a sentence with one word changed at about 0.95, so a wrong measure or a wrong entity passes. The numeric check catches a wrong number only. This breaks "no silent acceptance of unsupported claims". It would also inflate the graded pass rate with false verifications.
- **It still fails correct Claims.** Abbreviations (g-qu-02) and ordinary Writer paraphrases score 0.68-0.82 against the exact sentence they came from. Granularity cannot fix that; it needs semantic similarity.
- **Clause splitting fixes g-ma-02 only by luck of punctuation.** The same fact written as two sentences behaves differently, and the wrong-measure case in its own sentence still passes. It is the fragile, case-tuned workaround the rule rules out.
- **Patching the embedder or the threshold** is ruled out: no temporary embedder that T7 replaces, and no change to the 0.85 threshold without approval.

A naive sentence-level `fact_check` was simulated by monkeypatching it from the scratchpad only. It made the copied-word-for-word case pass, and it made **2 of the 3 guard tests fail** (wrong measure and wrong entity verified). The guards below therefore catch exactly this regression.

## The dependency, and what each option does and does not cover

| Need | Hashing + sentence | Semantic embedder (fastembed `bge-small`, design.md section 10, "arrives with T7/T8") | Model entailment on the matched sentence |
|---|---|---|---|
| Correct Claim inside a long paragraph | yes | expected, if matched per sentence | yes |
| Paraphrase and abbreviation (g-qu-02) | no | expected (not measured: no model is available offline) | yes |
| Wrong measure or entity with the right number (g-ma-02) | no (worse) | **unlikely**: embeddings score near-copies with one changed word highly | yes |

1. **The semantic embedder is necessary but probably not sufficient.** Similarity alone, whatever the embedder, does not reliably separate "R&D spend $90M" from "revenue $90M". The rubric's wording (cosine ≥ 0.85) cannot reject wrong-measure Claims by itself.
2. **Rejecting a wrong measure needs a judgment.** One way: after the 0.85 similarity gate, one light-model entailment call on the matched sentence, reusing T5's existing `ENTAIL_SYSTEM` / `ask_json` path, so no new infrastructure. This changes approved criterion 13 (ledger) and T6's "no model is called". It is an owner design decision and adds a paid call per sampled Claim (about 6-8 calls per Report).
3. **Scope.** T7's issue (#8) does not list the embedder in its acceptance criteria; only `design.md` places fastembed with T7/T8. The owner should confirm which ticket owns it.

**Owner decisions needed before F2 can proceed:**
- (a) Bring the fastembed embedder forward (new dependency and model download), or wait for T7/T8.
- (b) Approve or reject the entailment step that rejects wrong-measure Claims (a design change to criterion 13).
- (c) Matching per sentence, together with (a) or (b), not alone.

## Regression tests added (`tests/pipeline/test_f2_fact_check_regression.py`)

These go through the real `fact_check()` with a re-fetched page.

- **Known F2 failures, `xfail(strict=True, raises=AssertionError)`:**
  - 3 correct Claims rejected: g-qu-02, a Claim copied word for word inside a paragraph, a Writer paraphrase.
  - 1 wrong-measure Claim accepted (g-ma-02).

  When a fix makes one of them pass, the suite fails until the marker is removed.
- **Guards (pass today):** wrong measure, wrong entity and wrong year, each in its own sentence, must stay `unverified`.

## Verification (offline, no paid call)

| Command | Result |
|---|---|
| `uv run pytest tests/pipeline/test_f2_fact_check_regression.py -v` | 3 passed, 4 xfailed |
| Same file with the sentence-level simulation (scratchpad plugin) | 2 guards failed, 1 strict xfail unexpectedly passed (the intended signal) |
| `uv run pytest tests/pipeline/test_t6_writer_fact_check.py -q` | 31 passed (sampling, provenance, citations, re-fetch unchanged) |
| `uv run lint-imports` | 4 kept, 0 broken |
| `uv run pytest -m baseline` / `-m replay` / `-m scripted` | 20 / 76 / 261 passed (+4 xfailed) |
| `uv run pytest` (all non-live) | 1095 passed, 4 xfailed, 5 deselected (live) |
| `uv run insightforge-agent demo` | exit 0 |

Sampling, provenance, citations and re-fetch are untouched (no production change). Their existing T6 tests pass unchanged.
