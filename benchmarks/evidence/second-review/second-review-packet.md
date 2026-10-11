# Independent second review packet

You are the **independent second reviewer**. Please judge these 10 items yourself, on your own,
without asking anyone for help and without looking at any other record of these cases.

**What to do**
1. Read the labeling rubric below.
2. Read each item (S001 to S010) and choose one of its permitted labels.
3. Open the spreadsheet `second-review-decisions.csv` (Excel, Google Sheets, Numbers or any text
   editor). For each row fill in:
   - `label`: your chosen label, exactly as written (for example `DIFFERENT_FACT`);
   - `reviewer`: your full name, as you want it recorded;
   - `reviewed_on`: the real date you made the judgment, as `YYYY-MM-DD` (for example
     `2026-10-19`). If your spreadsheet changes the format, set the column to Text first;
   - `uncertain` (type `yes` if unsure) and `note` (why). Optional, but a `yes` needs a note.
4. Save the file as CSV with the same name and columns, and send it back to the owner.

Do not change `review_id` or `permitted_labels`. Do not leave `label` blank: if you are unsure,
give your best label and mark it uncertain.


## Labeling rubric

Read each item only for what it shows. Relative dates ("last quarter", "this year") are resolved
against the date of the text they appear in. Judge the meaning, not the wording.

**Fact comparison**
- `SAME_FACT`: one fact, worded, rounded or ordered differently. Both statements are true or
  false together. One statement may carry extra context (a comparison, a description, a
  background detail) and still be `SAME_FACT`, provided the claim the other statement makes is
  contained in it unchanged. Extra context that changes the claim's entity, measure, period or
  scope is a different claim: `DIFFERENT_FACT`.
- `DIFFERENT_FACT`: they concern different things (measure, period, scope, entity or
  relationship). Both can be true.
- `CONTRADICTORY`: same entity, measure, period and scope, but they cannot both be true
  (opposite polarity or direction, different values of a single-valued predicate, a factor of
  1,000 apart).

**Passage support**
- `SUPPORTED`: the Passage states what the statement says, at the statement's own precision.
- `CONTRADICTED`: the Passage states a conflicting value for the same entity, measure and period.
- `INSUFFICIENT_EVIDENCE`: the Passage is silent on the statement, only forecasts or recalls
  another period, or is less precise than the statement.

**Grounding**
- `GROUNDED`: the Passage (for a quoted-span item, the quoted span read in the Passage's
  context) says what the statement says, with the right entity, measure, number, unit and period.
- `NOT_GROUNDED`: anything else: the span is absent, says something different, or is about
  another entity, measure, unit or period.

**Source independence** (two Sources are independent only if they have different domains and
neither cites the other)
- `INDEPENDENT`: separate reporting; neither reproduces, cites or derives from the other.
- `DEPENDENT`: one reproduces, syndicates, cites or evidently derives from the other.
- `UNDETERMINED`: the texts do not allow either answer.

If you cannot decide, still pick your best label, mark the row `uncertain` and say why in the
note. Do not leave it blank.

---

## S001

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Biotherapeutics is headquartered in Leiden.

**Statement B**
> Orrin Bio is based in Leiden.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## S002

**Judgment type:** Source independence

**Question:** Are Source A and Source B independent of each other?

**Source A URL:** https://www.dealwatch.example/mergers/helix-brightmart

**Source A text**
> Helix Foods completed its $1.2 billion purchase of Brightmart in 2023, two people familiar with the matter said.

**Source B URL:** https://www.groceryweekly.example/retail/brightmart-new-owner

**Source B text**
> Brightmart's store managers were told in 2023 that Helix Foods had closed its $1.2 billion acquisition, according to an internal memo seen by Grocery Weekly.

**Permitted labels:** `INDEPENDENT` | `DEPENDENT` | `UNDETERMINED`

---

## S003

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Kestrel Air carried 31 million passengers in 2024.

**Passage**
> Kestrel Air said it carried 31 million passengers in 2024, up 6% on the year before.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## S004

**Judgment type:** Source independence

**Question:** Are Source A and Source B independent of each other?

**Source A URL:** https://wirenews.example/markets/kestrel-results

**Source A text**
> (Wire News) - Kestrel Air said on Monday it carried 31 million passengers in 2024, up 6%. Reporting by A. Reporter; editing by B. Editor.

**Source B URL:** https://www.metrotimes.example/economy/kestrel-traffic

**Source B text**
> Kestrel Air said on Monday it carried 31 million passengers in 2024, up 6%. Reporting by A. Reporter; editing by B. Editor. (Wire News)

**Permitted labels:** `INDEPENDENT` | `DEPENDENT` | `UNDETERMINED`

---

## S005

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking an Evidence statement and the span quoted for it.

**Statement**
> Helix Foods cut prices by 10% to win market share.

**Quoted span**
> prices to rise 10% next year

**Passage**
> Helix Foods said it expects prices to rise 10% next year as input costs climb.

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`

---

## S006

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Bio's drug OB-12 entered a Phase 2 trial in 2024.

**Statement B**
> Orrin Bio's drug OB-21 entered a Phase 2 trial in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## S007

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Tessera Energy's 2024 revenue was $4.27 billion.

**Statement B**
> Tessera Energy's revenue came to about $4.3 billion in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## S008

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Bio had 310 employees at the end of 2022.

**Statement B**
> Orrin Bio had 310 employees at the end of 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## S009

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Tessera Energy's 2024 revenue was $4.27 billion.

**Passage**
> Tessera Energy expects 2024 revenue of about $4.27 billion, according to guidance issued in January 2024.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## S010

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking an Evidence statement and the span quoted for it.

**Statement**
> Helix Foods reported fiscal 2024 revenue of $2.4 billion, about 3% higher than the year before.

**Quoted span**
> fiscal 2024 revenue came to US$2.4bn, roughly 3% above the prior year

**Passage**
> Helix Foods' fiscal 2024 revenue came to US$2.4bn, roughly 3% above the prior year.

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`
