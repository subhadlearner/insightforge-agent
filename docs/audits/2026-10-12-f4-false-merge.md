# F4: false merges on the merge key (2026-10-12)

**Scope:** audit finding F4 (`2026-10-11-evidence-quality-audit.md`). Branch `f4-false-merge-hardening` from `84cfb41`. No paid or live call. F3, T8 and the discontinued semantic-resolver work (M1.6-M5) are out of scope.

## 1. Baseline merge-key failure (the audit's finding)

The deterministic baseline (`m1.3-2`) gave equal `merge_identity` keys for all 5 judged DIFFERENT_FACT PAIR cases:

| Case | Why the keys were equal |
|---|---|
| `p-ta-02` | present tense, no period; published 2021 and 2025 |
| `p-ta-03` | "this year" (unreadable), published 2023 and 2024 |
| `p-ps-01` | "fiscal 2023" / "fiscal 2024": both unreadable, so both UNKNOWN |
| `p-ps-04` | "end of 2022" / "end of 2024": the same |
| `p-fm-02` | explicit `2024` on both sides; two different companies named Aldine Capital |

This shows only that the **keys** collide. It does not show what the full pipeline then does, because entailment and the literal check run after Merge.

## 2. Production Synthesis behaviour (Phase 1, before any change)

`tests/pipeline/test_f4_false_merge.py` drives the real `synthesize()`. Each case is two web Sources on different domains, credibility 0.9. Drafting is scripted with the reviewed statements and structured fields, and the non-canonical period wording is returned as the model would give it. Entailment is scripted as the semantically correct verdict. Pinned in commit `cbad0a7`:

| Case | Drafted | After Merge | Final items (supporting) | Confidence | Effect |
|---|---|---|---|---|---|
| `p-ta-02` | 2 | 1 (a+b) | 1 (a, b) | **HIGH** | **false corroboration**: 2021 and 2025 headcounts as one fact |
| `p-ta-03` | 2 | 1 (a+b) | 1 (a, b) | **HIGH** | **false corroboration**: 2023 and 2024 projections as one |
| `p-ps-01` | 2 | 1 (a+b) | 1 (a) | MEDIUM | the fiscal-2024 fact **lost, unlogged** (literal check: `2023` not in Passage b) |
| `p-ps-04` | 2 | 1 (a+b) | 1 (a) | MEDIUM | the end-2024 fact **lost, unlogged** (`2022` not in Passage b) |
| `p-fm-02` | 2 | 1 (a+b) | 1 (a) | MEDIUM | the Houston lender's fact **lost, unlogged** (`London` not in Passage b) |
| `p-fm-02`, no descriptor in either statement | 2 | 1 (a+b) | 1 (a, b) | **HIGH** | **false corroboration**: nothing lexical separates the two companies |

So the existing checks prevent false corroboration only when the kept statement carries a word or figure that the other Passage lacks. Otherwise they do not, and in every case one legitimate fact disappears. Which side survives depends on drafting order.

## 3. Defect fixed (Phase 2)

**Rule** (`pipeline/synthesize.merge_identity`). A structured candidate whose normalised stated period is UNKNOWN is also keyed by `observations=<sorted, de-duplicated Observation IDs it cites>`. It merges only with candidates citing exactly the same set. Explicit-period and statement-keyed identities are byte-for-byte unchanged. Observation IDs are content-derived, so Evidence IDs stay deterministic.

**A candidate citing several Observations** is kept whole. Merge never splits it, and it is judged against all its Passages. It merges with a candidate citing the same set, and not with a subset or superset. Unequal overlapping sets therefore stay separate items. That can duplicate, but never merges falsely.

After the change:

| Case | After Merge | Final items | Confidence | Status |
|---|---|---|---|---|
| `p-ta-02`, `p-ta-03` | 2 | 2 (a), (b), both UNKNOWN and temporally ambiguous | MEDIUM, MEDIUM | **fixed**: no false HIGH |
| `p-ps-01`, `p-ps-04` | 2 | 2 (a), (b), both statements kept | MEDIUM, MEDIUM | **fixed**: no lost fact |
| `p-fm-02` (both variants) | 1 | unchanged from section 2 | MEDIUM / HIGH | **not fixed** (section 4) |

The `p-ps` fixes depend on the model returning non-canonical wording. If it returns `2023` and `2024`, as the prompt asks, the keys already differed before F4.

**Controls** (same file, all green): duplicates within one Observation (several Passages, an unreadable period) still deduplicate; one Observation found by two Sub-tasks still merges into the more important one; the same explicit period across two independent Observations still corroborates to HIGH; different explicit periods stay distinct; IDs are deterministic and differ only for UNKNOWN-period candidates from different Observations. Existing contradiction, Confidence, provenance and Synthesis tests are unchanged and green. The one intended golden change is the UNKNOWN row in `tests/pipeline/test_t5_extractions.py`.

## 4. Limitations deliberately retained

- **`p-fm-02`, same-name different entities with an explicit period.** The merge key has no entity disambiguation. With descriptors in the statements the literal check blocks false corroboration, but one company's fact is lost. Without descriptors, both Passages state the statement and the item is HIGH. No small deterministic rule was found that is safe for legitimate corroboration:
  - Keying on the statement's capitalised words would split true paraphrases that name a city, an outlet or a product.
  - Not merging a candidate whose Passages fail the literal check against the kept statement would keep the lost fact. But it needs Passage reads during Merge and order-dependent ID disambiguation, and it does not touch the descriptor-free HIGH, which is the risk that matters.

  No semantic resolver was built. **Recommendation: put it on the T16 watchlist.** A sample Brief about a common company name would show whether this happens in practice.
- **Undated Observations grouped by the drafting model.** One drafted candidate that itself cites two undated Observations is judged as drafted and can reach HIGH. That is a drafting judgment, not Merge (test `..._merges_only_with_the_same_set`).
- **Statement-keyed candidates** (no entity, predicate or value) are still keyed by the normalised statement alone. Identical undated wording from two Observations still merges (test `test_unstructured_candidates_are_still_keyed_by_statement_alone`). This is outside the F4 rule as specified.
- **A model-stated explicit period that the Passage does not state** is rejected to UNKNOWN only at validation, after Merge. So two Observations given the same invented period still merge.

## 5. Benchmark

Baseline version `m1.3-2` -> **`m1.3-3`**. The two candidates of a PAIR count as separate Observations, `a` and `b`.

| | `m1.3-2` | `m1.3-3` |
|---|---|---|
| PAIR judged | 7 / 24 | 2 / 24 |
| judged DIFFERENT_FACT merged (false merge) | 5 / 5 | 1 / 1 (`p-fm-02`) |
| SAME_FACT merged | 2 / 8 (`p-pq-03`, `p-ta-01`) | 1 / 8 (`p-pq-03`) |

`p-ta-01` ("last quarter" vs "Q4 2024", both unreadable) had merged only because two UNKNOWN periods were equal. Its loss is the honest result of the rule. `tests/benchmark/test_baseline.py::test_the_reviewed_f4_cases_follow_the_production_merge_rule` pins all six outcomes against the reviewed dataset.
