# Owner blind review: instructions

For issue [#34](https://github.com/subhadlearner/insightforge-agent/issues/34). You label the 48 pilot cases yourself, **without seeing the proposed labels**. You edit one spreadsheet. You never touch Python or `cases.jsonl`.

All commands run from the repository root. Everything is offline; no model is called.

## What you need

| File | What it is | Your action |
|---|---|---|
| `benchmarks/evidence/review/owner-review-packet.md` | 48 items (R001-R048), rubric, permitted labels | **Read it** |
| `benchmarks/evidence/review/owner-decisions.csv` | One row per item, blank | **Fill it in** (Excel, LibreOffice or any text editor) |
| `benchmarks/evidence/review/sealed/review-id-map.json` | R-number to case id | **Do not open** until you finalize |
| `benchmarks/evidence/cases.jsonl` | Proposed labels and rationales | **Do not open** until you finalize |

The packet and sheet are already generated and committed. Regenerating is refused if they exist, so your work cannot be overwritten.

## Steps

1. **Read the rubric** at the top of the packet. It is the labeling guideline you are applying.
2. **Open the packet** (`code benchmarks/evidence/review/owner-review-packet.md`, or any Markdown viewer).
3. **Open the sheet** `benchmarks/evidence/review/owner-decisions.csv`. Columns:

   | Column | You |
   |---|---|
   | `review_id` | leave alone |
   | `permitted_labels` | leave alone (a reminder) |
   | `label` | **required**: one permitted label, e.g. `DIFFERENT_FACT` |
   | `uncertain` | type `yes` if you are unsure, else leave blank |
   | `note` | optional reasoning; **required when `uncertain` is `yes`** |
   | `reviewer` | **required**: your name, as you want it recorded |
   | `reviewed_on` | **required**: the real date you judged it, `YYYY-MM-DD` |

   Never leave `label` blank for a hard item: pick your best label, set `uncertain` to `yes`, say why in `note`.
4. **Validate as you go** (safe to run any time):
   ```
   uv run insightforge-agent bench review validate --allow-incomplete
   ```
   It lists problems per row and which rows are not yet labelled. It never shows proposed labels.
5. **Validate when finished** (every row labelled, no problems):
   ```
   uv run insightforge-agent bench review validate
   ```
   It must print `OK`.
6. **Finalize (freeze) the blind review.** This is deliberate and not undoable by the tool:
   ```
   uv run insightforge-agent bench review finalize
   ```
   It writes `benchmarks/evidence/review/owner-decisions.frozen.json` (your labels, notes, name and dates, plus a fingerprint of the sheet). It refuses to overwrite an existing frozen file. Do not edit the sheet afterwards; later steps detect it.
7. **Send back for comparison.** Commit the filled sheet and the frozen file on a feature branch (never `main`) and open a PR, or tell the assistant they are ready:
   ```
   git switch -c m1.5-owner-decisions
   git add benchmarks/evidence/review/owner-decisions.csv benchmarks/evidence/review/owner-decisions.frozen.json
   git commit -m "M1.5: owner blind decisions"
   ```
   Only now may the comparison run:
   ```
   uv run insightforge-agent bench review compare
   ```
   It lists every case where your label differs from the proposed one. It changes nothing and resolves nothing. Disagreements are yours to adjudicate (see [review-integration-plan.md](review-integration-plan.md)).

## Synthetic example (not a benchmark case)

> **Statement A:** Zephyra Tools shipped 9,000 drills in 2021.
> **Statement B:** Zephyra Tools shipped 9,000 drills in 2022.
> Permitted labels: `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

Different years, so both can be true: `DIFFERENT_FACT`. A filled row (labelled `R000` here because it is not in the sheet):

```
review_id,permitted_labels,label,uncertain,note,reviewer,reviewed_on
R000,SAME_FACT|DIFFERENT_FACT|CONTRADICTORY,DIFFERENT_FACT,yes,"Might be a typo for the same year",A. Owner,2026-10-12
```

## Second reviewer

About 10 cases (about 20%) are judged again, blind, by a **different person**, preferably at least 7 days after your review; the actual gap is recorded from the two real dates. They get a packet like yours (neutral ids, no labels, no yours), fill in the same kind of sheet, and the tool compares only after both are frozen. With about 10 cases the agreement figure is weak evidence and will be reported with that caveat. Details: [review-integration-plan.md](review-integration-plan.md).

## Rules the tooling enforces

- Nothing is ever filled in for you: no reviewer name, no date, no label.
- Labels must belong to the item's judgment type; dates must be real and not in the future.
- A duplicate or unknown `review_id`, or a missing row, fails validation.
- `compare` is refused until `finalize` has run, and refuses if the frozen file or the proposed cases differ from what was sealed at preparation.
- The 48 cases stay `proposed` until the integration step, which needs your decisions.
