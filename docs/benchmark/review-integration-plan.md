# M1.5 review integration plan

**Status: PROPOSED.** A plan for the step *after* the owner's blind review; none of it is implemented in the preparation PR (issue [#34](https://github.com/subhadlearner/insightforge-agent/issues/34), parent [#26](https://github.com/subhadlearner/insightforge-agent/issues/26)). Owner instructions: [owner-review.md](owner-review.md).

## What exists after preparation

`bench review prepare | validate | finalize | compare` (`src/insightforge_agent/benchmark/review.py`). The frozen owner record holds no proposed label; the proposed label stays in `cases.jsonl` as `expected`. Nothing writes to `cases.jsonl`.

## 1. Reconcile owner and proposed labels

Run `bench review compare`. Per case:

- **Owner label = proposed label** → candidate for `reviewed`.
- **They differ** → candidate for `disputed`. Neither is overwritten and neither wins automatically. The owner adjudicates it separately, in writing, after seeing both labels, and rereads the labeling rubric first (see criterion 6 below).
- **Owner marked `uncertain`** → listed for attention even if it agrees.

The owner's judgment and the proposed label are different judges. Their agreement is **not** reported as kappa or "human agreement": the proposed labels were drafted by the author, not by an independent human. It is a discrepancy count only.

## 2. The contract change (smallest sufficient)

`BenchmarkCase` today has `expected` (the label scored against), `reviewer`/`reviewed_at` (the review of `expected`), `dispute_note`, and `second_reviewer`/`second_reviewed_at`/`second_label`. Writing the owner's label into `expected` would destroy the proposed label; adding nothing would lose the owner's independent judgment when they differ. Proposed addition, three optional fields, all `None` while `proposed`:

| Field | Holds |
|---|---|
| `proposed_label` | the original proposed label, copied from `expected` before anything changes |
| `first_label` | the owner's blind label (with `reviewer`/`reviewed_at` as its reviewer and date) |
| `adjudication_note` | the written rationale for the final `expected` |

Rules: `disputed` keeps `expected == proposed_label` untouched, needs `first_label != proposed_label` or a second-review disagreement, and is **excluded from qualification metrics** (already true). Adjudication sets `expected`, `adjudication_note`, and `label_status=reviewed`; both original labels stay in `proposed_label` and `first_label`. `reviewed` otherwise needs `first_label == expected`. The existing `second_*` fields are kept as is. The relaxation that lets a reviewed case differ from its second label only via adjudication is part of this change and needs its own tests.

The test `test_every_label_starts_proposed_with_no_review_data` is **evolved, not deleted**: it becomes "every case is `proposed`, or carries review data consistent with the frozen owner record (`first_label`, `reviewer`, `reviewed_at` all match it)". The 48-case composition test is untouched.

A small import command (`bench review apply`) will read the frozen record and write a *new* cases file (not in place), refusing if a case already carries review data. It uses the actual `reviewed_on` from the record, never today's date.

## 3. Second review (blind, ~20%)

Preparation, collection and freezing are implemented: see [second-review.md](second-review.md). The agreement report below is not.

- **Sample:** about 10 cases, drawn with a fixed seed after the owner is frozen, stratified across the four judgment types and across positive and negative owner labels, with the critical cases preferred where a stratum allows. Never chosen by looking at disagreements.
- **Blind:** a second packet built by the same code with its own neutral ids and ordering; it shows no label from the owner or the proposal. A different person; if it is the same person as the owner the tool rejects it (the schema already requires `second_reviewer != reviewer`).
- **Gap:** preferably at least 7 days after the owner's `reviewed_on`. The report records the actual interval in days, computed from the two real dates (`second_reviewed_at - reviewed_at`), and says so when it is under 7.
- **Import:** same validate / freeze / never-overwrite pattern as the owner sheet; stored as `second_label`, `second_reviewer`, `second_reviewed_at`.

## 4. Agreement statistics

Computed before any adjudication, from `first_label` vs `second_label`, using `stats.cohens_kappa` (undefined, never 0, when there are no pairs or chance agreement is 1):

- Raw agreement, with sample size, and the Wilson 95% interval (`stats.wilson_interval`).
- Cohen's kappa, with the reason when undefined.
- **Per judgment type where supportable.** With about 10 cases the per-type counts are about 2 to 5; kappa on that is undefined or wildly unstable (a single disagreement swings it). Below a stated minimum per-type n the report prints raw agreement and the counts only, and says why kappa is withheld.
- A pooled kappa across types mixes four different label sets and three positive/negative balances. It is printed only with that qualification beside it, never as the headline. About 10 observations cannot give a statistically strong estimate; the report says so.
- Kappa below 0.8 triggers an investigation (read the disagreements, the rubric), not an automatic failure.

## 5. Disagreement handling and exclusions

- Any owner/proposed or owner/second disagreement → `disputed`, both labels preserved, excluded from headline qualification metrics until adjudicated with a written `adjudication_note`.
- Unresolved disputed cases never enter headline metrics; the report counts them.
- A disagreement on a false-merge (PAIR positive predicted for a negative), false-support, or false-HIGH case requires the labeling guidelines to be reviewed before the dataset is finalised (issue criterion 6); the report lists these separately.
- "Model agreement" (optional blind model labelling, criterion 2) is a separate, clearly named figure and is never added to the human numbers. It is out of scope here (offline, no inference).

## 6. Final reviewed benchmark

After adjudication, `bench review apply` writes the reviewed cases file; `bench validate` and the evolved dataset test pass; the PR reports reviewed/disputed counts, kappa with caveats, and the interval. The pilot's methodology status is then updated by #35, which owns the methodology approval.

## Issue #34 criteria and status after the preparation PR

| Criterion | Status |
|---|---|
| Owner reviews every critical case and every disputed label; `proposed` → `reviewed` | **Not done**: needs the owner's decisions |
| Optional blind model labelling | Not done; optional, out of scope offline |
| Blind second review (~20%) with actual interval | **Not done**: needs the second human; plan above |
| Disagreements → `disputed`, adjudicated, excluded | **Not done**: no disagreements exist yet; plan above |
| Kappa, raw agreement, sample size before adjudication | **Not done**: needs both reviews; helpers exist in `stats.py` |
| Guideline review on false-merge/support/HIGH disagreements | **Not done**: depends on disputes |
