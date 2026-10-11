# Second-human review: preparation and instructions

For issue [#34](https://github.com/subhadlearner/insightforge-agent/issues/34) (blind second review of about 20% of the pilot cases). This covers **preparation, collection and freezing only**. Agreement statistics, adjudication and benchmark integration are the next milestone ([review-integration-plan.md](review-integration-plan.md) §3-5); nothing here computes them.

## The sample (10 cases)

Drawn by `bench review second-prepare` from the owner's **frozen original blind review** (`benchmarks/evidence/review/owner-decisions.frozen.json`). It never reads the later adjudication, the owner/proposed disagreements or any baseline prediction.

| Judgment type | Cases | Positive | Negative |
|---|---|---|---|
| PAIR | 4 | 2 | 2 |
| SUPPORT | 2 | 1 | 1 |
| GROUND | 2 | 1 | 1 |
| INDEPENDENCE | 2 | 1 | 1 |

"Positive" means the owner's original label is the type's positive class (SAME_FACT, SUPPORTED, GROUNDED, INDEPENDENT); every other label is negative. Rules (fixed, recorded in the sealed manifest):

1. Per type the allocation above is fixed and split as evenly as possible between positive and negative (a short stratum passes its shortfall to the other).
2. Within a stratum, critical cases come first, then cases ordered by `sha256(seed + case id)`; the first *k* are taken.
3. Seed: `insightforge-m1.5-second-review-v1`. Same inputs always give the same sample.
4. Reviewer order and the new ids (S001-S010) come from `sha256(seed + "order:" + case id)`, independent of stratum.

Ten observations cannot give a statistically strong agreement estimate; per-type counts of 2 or less will make per-type kappa undefined or unstable. The later report must say so.

## Files

| File | Who sees it |
|---|---|
| `benchmarks/evidence/second-review/second-review-packet.md` | **second reviewer** (self-contained: instructions, rubric, 10 items) |
| `benchmarks/evidence/second-review/second-review-decisions.csv` | **second reviewer** (blank sheet) |
| `benchmarks/evidence/second-review/sealed/second-review-map.json` | owner only: S-id to case id, strata, seed, rules, digest of the owner's frozen review |
| `benchmarks/evidence/second-review/second-review.frozen.json` | created by `second-finalize`; owner only |

The reviewer receives **only the first two files**, as copies outside the repository (email, shared drive). They have no access to the repository, the owner's sheet, the proposed labels or the sealed folder.

## Instructions for the second reviewer (also printed at the top of the packet)

You need the two files and a program that opens `.md` and `.csv` files (any text editor, Excel, Google Sheets or Numbers). You do not run any code.

1. Judge on your own. Do not ask the owner or anyone else, and do not look the cases up.
2. Open `second-review-packet.md`. Read the labeling rubric, then each item S001-S010.
3. Open `second-review-decisions.csv`. For each row fill in:
   - `label`: one of the item's permitted labels, exactly as written;
   - `reviewer`: your full name;
   - `reviewed_on`: the real date you judged, `YYYY-MM-DD` (spreadsheets sometimes rewrite dates; set the column to Text first, or check the saved file);
   - optionally `uncertain` (`yes`) and `note`. A `yes` needs a note.
4. Never leave `label` blank. Unsure: best label plus `uncertain` = `yes` and a reason.
5. Save as CSV (same file name, same columns) and send it back to the owner.

Synthetic example (not a benchmark case): *Statement A: "Zephyra Tools shipped 9,000 drills in 2021." Statement B: "... in 2022."* Different years, both can be true: `DIFFERENT_FACT`.

## Owner steps

Run from the repository root. Preparation (already done in the PR):

```
uv run insightforge-agent bench review second-prepare
```

It stops with an error, and writes nothing, if the owner's CSV, frozen record or id mapping is missing or modified; it never recreates human judgments. It refuses to overwrite an existing second-review file.

When the reviewer returns the CSV:

1. Save it over `benchmarks/evidence/second-review/second-review-decisions.csv`. Do not edit it.
2. Check it: `uv run insightforge-agent bench review second-validate`. It must print `OK`. It rejects an unknown label, a missing row, a date in the future or before your own review of the case, more than one reviewer name, and a reviewer name equal to yours (case and spacing ignored; this cannot detect a different spelling of the same person, so confirm the reviewer is a different human).
3. Freeze it: `uv run insightforge-agent bench review second-finalize`. It writes `second-review.frozen.json` (labels, notes, reviewer, dates, your original review date per case and the **actual interval in days**), contains no owner label, and refuses to overwrite.
4. Later, or whenever you doubt a file: `uv run insightforge-agent bench review second-verify`. It rebuilds the record from the original sheet and rejects any altered, missing, extra or duplicate decision, an edited sheet, or a changed owner record.
5. Keep the interval: the preferred gap is 7 days or more after your review (2026-10-11, so on or after 2026-10-18). A shorter gap is allowed but is recorded and counted (`under_preferred_gap`), never hidden.
6. Commit the sheet and frozen file on a feature branch and open a PR. Do not tell the reviewer your labels, and do not open the sealed folder with the reviewer present.

`second-finalize` and `second-verify` need your own `owner-decisions.csv`, `owner-decisions.frozen.json` and `sealed/review-id-map.json` in `benchmarks/evidence/review/`.

## Guarantees and limits

- The second reviewer's decisions are kept apart from the owner's, the proposed labels and the adjudication. The frozen second record holds only the reviewer's own judgments and the owner's review *dates*.
- Nothing is filled in automatically: no reviewer name, no date, no label.
- A model never stands in for the second human. Any model labelling, if done later, is reported as model agreement.
- Not done here: agreement, kappa, disputes, adjudication, changes to `cases.jsonl`.
