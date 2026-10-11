# Owner blind review packet

48 items. Record each label in `benchmarks/evidence/review/owner-decisions.csv`, not here. This packet shows no proposed label, rationale, category or flag; do not look them up before you finish.

## Labeling rubric

Read each item only for what it shows. Relative dates ("last quarter", "this year") are resolved
against the date of the text they appear in. Judge the meaning, not the wording.

**Fact comparison**
- `SAME_FACT`: one fact, worded, rounded or ordered differently. Both statements are true or
  false together.
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

## R001

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Kestrel Air's cargo division had a net margin of 6.2% in 2024.

**Statement B**
> Kestrel Air had a group net margin of 6.2% in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R002

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Helix Foods acquired Brightmart in 2023 for $1.2 billion.

**Statement B**
> Brightmart was bought by Helix Foods in 2023 for $1.2 billion.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R003

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Kestrel Air Holdings reported 2024 revenue of $5.0 billion.

**Statement B**
> KST posted 2024 sales of $5.0 billion.

**Passage B**
> Kestrel Air Holdings (NYSE: KST) ended the year with record results. KST posted 2024 sales of $5.0 billion.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R004

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Tessera Energy's 2024 revenue was $4.27 billion.

**Passage**
> Tessera Energy expects 2024 revenue of about $4.27 billion, according to guidance issued in January 2024.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R005

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking an Evidence statement and the span quoted for it.

**Statement**
> Brightmart's store count fell to 1,200.

**Quoted span**
> store count reached a record 1,200

**Passage**
> Brightmart's store count reached a record 1,200 at the end of the year.

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`

---

## R006

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Norvale Motors' 2024 net profit rose 12%.

**Passage**
> Norvale Motors' 2024 net profit rose 21%.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R007

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Kestrel Air carried 31 million passengers in 2024.

**Passage**
> Kestrel Air operates 120 aircraft, and its fleet has an average age of nine years.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R008

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

## R009

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Brightmart's revenue was $900 million last quarter.

**Passage A**
> Published 15 January 2025. Brightmart's revenue was $900 million last quarter.

**Statement B**
> Brightmart's revenue was $900 million in the fourth quarter of 2024.

**Passage B**
> Brightmart reported fourth-quarter 2024 revenue of $900 million.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R010

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

## R011

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

## R012

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Kestrel Air will carry 35 million passengers this year.

**Passage A**
> Published 12 February 2023. Kestrel Air will carry 35 million passengers this year.

**Statement B**
> Kestrel Air will carry 35 million passengers this year.

**Passage B**
> Published 20 February 2024. Kestrel Air will carry 35 million passengers this year.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R013

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Helix Foods' revenue was $2.1 billion in fiscal 2023.

**Statement B**
> Helix Foods' revenue was $2.1 billion in fiscal 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R014

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Helix Foods' 2024 revenue was $2.4 billion.

**Passage**
> Helix Foods, founded in 1987, is headquartered in Leeds and sells frozen meals in 14 countries.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R015

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Brightmart opened 40 new stores in 2024.

**Statement B**
> Brightmart closed 40 stores in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R016

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Brightmart opened 40 stores in 2024.

**Passage**
> Brightmart opened 40 stores in 2023.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R017

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Brightmart will open 60 stores in 2025.

**Passage**
> Brightmart opened 60 stores in 2024.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R018

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking an Evidence statement and the span quoted for it.

**Statement**
> Kestrel Air's cargo division had a net margin of 6.2% in 2024.

**Quoted span**
> Net margin 6.2%

**Passage**
> Net margin 6.2%

**Context (what the Passage is about)**
> Table 3: Cargo division results, fiscal 2024

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`

---

## R019

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Mara Quill is the chief executive of Orrin Bio as of 1 March 2025.

**Statement B**
> Jonas Pike is the chief executive of Orrin Bio as of 1 March 2025.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R020

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Aldine Capital, the London asset manager, closed a $400 million fund in 2024.

**Statement B**
> Aldine Capital, the Houston energy lender, closed a $400 million fund in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R021

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Kestrel Air's passenger traffic rose 8% in 2024.

**Statement B**
> Kestrel Air's passenger traffic fell 8% in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R022

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Kestrel Air operates 120 aircraft.

**Statement B**
> Kestrel Air has 120 aircraft on order.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R023

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Bio has 310 employees.

**Passage A**
> Published 3 May 2021. Orrin Bio has 310 employees and plans to expand.

**Statement B**
> Orrin Bio has 310 employees.

**Passage B**
> Published 9 June 2025. Orrin Bio has 310 employees after a round of layoffs.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R024

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Orrin Bio's OB-12 trial met its primary endpoint.

**Passage**
> Orrin Bio will present OB-12 trial results at the March conference.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R025

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Kestrel Air carried 31 million passengers in 2024.

**Passage**
> Kestrel Air said it carried 31 million passengers in 2024, up 6% on the year before.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R026

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Norvale Motors Inc. named Priya Nair chief financial officer in 2024.

**Statement B**
> Norvale appointed Priya Nair as CFO in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R027

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Helix Foods was profitable in fiscal 2024.

**Statement B**
> Helix Foods reported a net loss in fiscal 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R028

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Helix Foods' operating margin was 8.4% in 2024.

**Statement B**
> Helix Foods earned an operating margin of 8.4% in 2024, up from 7.9% a year earlier.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R029

**Judgment type:** Source independence

**Question:** Are Source A and Source B independent of each other?

**Source A URL:** https://www.tradejournal.example/aviation/kestrel-2024

**Source A text**
> Our analysis of Kestrel Air's annual filing shows 31 million passengers in 2024. Fuel hedging, not traffic, drove the margin gain, said analyst D. Okoro.

**Source B URL:** https://www.heraldlocal.example/business/kestrel-hub

**Source B text**
> At Kestrel Air's home hub we counted queues, spoke to staff and reviewed the airport authority's figures: about 31 million passengers passed through in 2024.

**Permitted labels:** `INDEPENDENT` | `DEPENDENT` | `UNDETERMINED`

---

## R030

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Bio had 310 employees at the end of 2022.

**Statement B**
> Orrin Bio had 310 employees at the end of 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R031

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Bio's drug OB-12 entered a Phase 2 trial in 2024.

**Statement B**
> Orrin Bio's drug OB-21 entered a Phase 2 trial in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R032

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Tessera Energy's 2024 revenue was $4.27 billion.

**Statement B**
> Tessera Energy's revenue came to about $4.3 billion in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R033

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Biotherapeutics is headquartered in Leiden.

**Statement B**
> Orrin Bio is based in Leiden.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R034

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking an Evidence statement and the span quoted for it.

**Statement**
> Norvale Motors delivered 410,000 vehicles in the third quarter.

**Quoted span**
> Tessera Motors delivered 410,000 vehicles in the third quarter

**Passage**
> Tessera Motors delivered 410,000 vehicles in the third quarter, ahead of rival Norvale Motors.

**Context (what the Passage is about)**
> Heading: Quarterly deliveries, Tessera Motors

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`

---

## R035

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Helix Foods' operating margin was 12% in 2024.

**Passage**
> Helix Foods' gross margin was 12% in 2024; its operating margin was 7%.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R036

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Tessera Energy's revenue was exactly $4.27 billion in 2024.

**Passage**
> Tessera Energy's revenue was about $4.3 billion in 2024.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R037

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Helix Foods supplies Brightmart with frozen meals.

**Statement B**
> Helix Foods competes with Brightmart in frozen meals.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R038

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Tessera Energy had 4.27 million active users in 2024.

**Statement B**
> Tessera Energy had 4.27 billion active users in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R039

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Helix Foods' 2024 net income was $210 million.

**Passage**
> Helix Foods forecasts 2024 net income of between $190 million and $210 million.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R040

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Brightmart's revenue was $900 million in the fourth quarter of 2024.

**Statement B**
> Brightmart's revenue was $900 million in the full year 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R041

**Judgment type:** Passage support

**Question:** Does the Passage support, contradict, or say too little about the statement?

**Statement**
> Orrin Bio named Jonas Pike as chief executive.

**Passage**
> Orrin Bio on Tuesday appointed Jonas Pike as its CEO, replacing Mara Quill.

**Permitted labels:** `SUPPORTED` | `CONTRADICTED` | `INSUFFICIENT_EVIDENCE`

---

## R042

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking a Report Claim (no quoted span).

**Statement**
> Tessera Energy's battery output reached 4.27 gigawatt-hours.

**Passage**
> Battery output hit 4.27 GWh at Tessera Energy's Nevada plant.

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`

---

## R043

**Judgment type:** Source independence

**Question:** Are Source A and Source B independent of each other?

**Source A URL:** https://www.dailyledger.example/news/helix-profit

**Source A text**
> By staff reporter. Helix Foods said profit rose 14% in fiscal 2024 on strong frozen-meal demand.

**Source B URL:** https://www.regionalpost.example/business/helix-profit

**Source B text**
> Helix Foods said profit rose 14% in fiscal 2024 on strong frozen-meal demand. Originally published by The Daily Ledger: https://www.dailyledger.example/news/helix-profit

**Permitted labels:** `INDEPENDENT` | `DEPENDENT` | `UNDETERMINED`

---

## R044

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Norvale Motors sold 4.27 million vehicles in 2024.

**Statement B**
> Norvale Motors reported revenue of $4.27 million in 2024.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R045

**Judgment type:** Fact comparison

**Question:** Do statements A and B state the same fact, different facts, or contradictory facts?

**Statement A**
> Orrin Bio employs about 310 people.

**Statement B**
> Orrin Bio's headcount is approximately 310.

**Permitted labels:** `SAME_FACT` | `DIFFERENT_FACT` | `CONTRADICTORY`

---

## R046

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking an Evidence statement and the span quoted for it.

**Statement**
> Brightmart opened 40 stores in 2024.

**Quoted span**
> opened 40 stores in 2024

**Passage**
> Brightmart opened 25 stores in 2024 and closed six.

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`

---

## R047

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

---

## R048

**Judgment type:** Grounding

**Question:** Is the statement grounded in the Passage?

Checking a Report Claim (no quoted span).

**Statement**
> Orrin Bio's R&D spend was $90 million.

**Passage**
> Orrin Bio's revenue was $90 million; its R&D spend was $40 million.

**Permitted labels:** `GROUNDED` | `NOT_GROUNDED`
