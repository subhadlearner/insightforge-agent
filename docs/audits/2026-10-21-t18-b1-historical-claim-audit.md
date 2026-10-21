# T18 B-1: Independent Historical Claim Audit

- **Prepared by:** Claude Code (implementer and evidence investigator), for GPT-6 and the owner
- **Repository:** `subhadlearner/insightforge-agent`, branch `t18-verification`, PR #74, `Refs #19`
- **Audited code:** `bac1a11cf183077ad134813979bbd456bbccf4b6` (the code B-1 run 3 ran)
- **Date:** 2026-10-20
- **Mode:** offline and read-only. No model calls, no repository changes.

## A. Executive summary

| Measure | Count |
|---|---|
| Claims audited | 27 |
| Adjudicated SUPPORTED | 24 |
| Adjudicated INSUFFICIENT_EVIDENCE | 3 |
| Adjudicated CONTRADICTED | 0 |
| Unresolved | 0 |
| Disagreements with the historical VERIFIED labels | 3: `run_ab1c836b61f4 1.5`, `run_ab1c836b61f4 2.9`, `run_06105d324454 3.3` |
| Disagreements with B-1 run 3's outcome | 1: `run_ab1c836b61f4 2.9` |

**Answer to the assignment's question.** The one remaining formal B-1 failure, `1.5`, is not a defect in the fact-checker. B-1 asked the improved verifier to reproduce an incorrect historical judgment, and run 3 gave the correct one. The historical VERIFIED labels are not ground truth: 3 of the 27 (11%) do not meet the approved F2 contract (`1.5`, `2.9`, `3.3`).

The audit also found a defect in the opposite direction that B-1 cannot see. Run 3 judged `2.9` SUPPORTED, but its Passage never says the figure is capex. Runs 1 and 2 did the same. The gate scored this as a pass because the historical label agreed with it.

All three questionable Claims have the same flaw: a qualifier (a market definition, a financial metric or a technology) taken from outside the cited Passage. In this class the judge's behaviour is inconsistent. Under the current, stricter instruction (runs 2 and 3), it accepted the unstated qualifier in **3 of 6** judgments (`1.5` once, `2.9` twice) and rejected it in 3 (`3.3` twice, `1.5` once).

Elsewhere the run 3 judge agrees with the audit on all 24 Claims labelled SUPPORTED. Every Claim that run 2 lost because of missing same-Passage context (`2.6`, Historical `3.1`) is now judged correctly. No case was found where sufficient permitted evidence existed but was not selected.

## Method

1. **Reconstruction:** `dump_27.py` rebuilds, for each verified Claim, exactly what B-1 run 3 sent the judge.
   - It uses the same functions the live harness calls at `bac1a11`: `verdicts` (including report-date stripping for Historical Claims), `screen` over all Passages of the verifier's matched Observation, `judged_candidates`, the cap-dropping loop and `entailment_request`.
   - The 27 rebuilt requests map one-to-one, in order, onto calls 49 to 75 of the run 3 record (76 of 76 calls accounted for).
   - Provider input tokens rise with the rebuilt request size (for example, 1,156 tokens for the 3-candidate request, the largest).
2. **Adjudication:** each Claim was labelled from the shown evidence alone, before its run outcomes were consulted (`adjudication.py`).
   - The standard is that one candidate, as shown (its own Passage only), must state every material assertion.
   - Not used: neighbouring Passages, the Source title, outside knowledge, or earlier verdicts.
   - Neighbouring Passages were read afterwards only to document where an unstated qualifier actually comes from, and to check whether any single Passage of the matched Observation could support the Claim (the evidence-selection question).
3. **Report generation:** `build_report.py` generates the tables and asserts that every quoted excerpt occurs in the text shown to the judge.


## B. Full 27-Claim audit

### B.1 Summary table

Original citation: the Passage the drafted Claim cites. Candidates: the Passages run 3 actually showed the judge, all from the verifier's matched (re-fetched) Observation; each was shown whole, because every one fits the 800-token context target. Run outcomes are from the saved records.

| # | Claim ref | Original citation | Candidates shown in run 3 | Audit label | Run 1 | Run 2 | Run 3 | Classification |
|---|---|---|---|---|---|---|---|---|
| 1 | `run_ab1c836b61f4 0.4` | `obs_da4f9a443c16a8fc` idx 126 | `obs_95d548d9310502b7` idx 8<br>`obs_95d548d9310502b7` idx 126 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 2 | `run_ab1c836b61f4 0.6` | `obs_f1726d1a2036ebb4` idx 36 | `obs_726ea7de504ee386` idx 36 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 3 | `run_ab1c836b61f4 1.5` | `obs_fa2a90a0de1c07d9` idx 348 | `obs_64b457150e160b35` idx 348 | **INSUFFICIENT_EVIDENCE** | SUPPORTED | SUPPORTED | INSUFFICIENT_EVIDENCE | Benchmark-label defect (historical VERIFIED). Runs 1-2: semantic judgment defect (permissive). Run 2 -> 3 change: unresolved between the added same-Passage sentences and model variability |
| 4 | `run_ab1c836b61f4 1.6` | `obs_a5ab96d128964c3f` idx 55 | `obs_a309549bbb2ab2a8` idx 55 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 5 | `run_ab1c836b61f4 2.7` | `obs_0c72da74b73b4069` idx 38 | `obs_4387e7642fbbfcb0` idx 38 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 6 | `run_ab1c836b61f4 2.9` | `obs_2a780a96eb8bc865` idx 21 | `obs_650992fa4cac8aa3` idx 21 | **INSUFFICIENT_EVIDENCE** | SUPPORTED | SUPPORTED | SUPPORTED | Benchmark-label defect (historical VERIFIED). Runs 1-3: semantic judgment defect (permissive), consistent in all three runs |
| 7 | `run_ab1c836b61f4 3.2` | `obs_f1726d1a2036ebb4` idx 289 | `obs_726ea7de504ee386` idx 289 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 8 | `run_0b45a66fd66d 0.0` | `obs_5e2476030808cf39` idx 0 | `obs_5e2476030808cf39` idx 0 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 9 | `run_0b45a66fd66d 0.1` | `obs_5e2476030808cf39` idx 0 | `obs_5e2476030808cf39` idx 0 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 10 | `run_0b45a66fd66d 0.3` | `obs_5e2476030808cf39` idx 10 | `obs_5e2476030808cf39` idx 10 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 11 | `run_0b45a66fd66d 2.5` | `obs_b9fe22278ef4eead` idx 297 | `obs_f70fec267f5837fe` idx 297 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 12 | `run_f388cc91cf2a 0.1` | `obs_887ba27ac7e4ef0e` idx 100 | `obs_6359dd66fd93dce0` idx 1<br>`obs_6359dd66fd93dce0` idx 100<br>`obs_6359dd66fd93dce0` idx 0 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 13 | `run_f388cc91cf2a 1.2` | `obs_f5fa159c6b113664` idx 1276 | `obs_4e52c80f98d3abe9` idx 1276 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 14 | `run_f388cc91cf2a 2.0` | `obs_354f2f805330c523` idx 1 | `obs_a41f771e79191b0d` idx 1 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 15 | `run_06105d324454 1.1` | `obs_e57ad6fb7bd81240` idx 21 | `obs_fef9895d6a3756dc` idx 21 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 16 | `run_06105d324454 1.7` | `obs_e57ad6fb7bd81240` idx 85 | `obs_fef9895d6a3756dc` idx 85 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 17 | `run_06105d324454 2.6` | `obs_b647a21595562e09` idx 110 | `obs_d74c1df2ec8da16a` idx 110 | **SUPPORTED** | SUPPORTED | INSUFFICIENT_EVIDENCE | SUPPORTED | Historical label correct. Run 1: SUPPORTED on the span alone, which lacks the LFP subject (semantic judgment defect, permissive, older instruction). Run 2: INSUFFICIENT on the span alone (evidence-selection defect: span without its Passage's subject), corrected by 773cb65; run 3 correct |
| 18 | `run_06105d324454 3.0` | `obs_77f645d65ab6f78a` idx 6 | `obs_dfceeb53b2b53844` idx 6<br>`obs_dfceeb53b2b53844` idx 25<br>`obs_dfceeb53b2b53844` idx 24 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 19 | `run_06105d324454 3.1` | `obs_77f645d65ab6f78a` idx 53 | `obs_dfceeb53b2b53844` idx 53 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 20 | `run_06105d324454 3.3` | `obs_d31bafe53dc36b95` idx 44 | `obs_a61d15cce53c78b8` idx 44 | **INSUFFICIENT_EVIDENCE** | SUPPORTED | INSUFFICIENT_EVIDENCE | INSUFFICIENT_EVIDENCE | Benchmark-label defect (historical VERIFIED; already adjudicated in stage_b1_verified_adjudication.json). Run 1: semantic judgment defect (permissive, older instruction). Runs 2-3 correct on the identical request |
| 21 | `run_06105d324454 3.6` | `obs_77f645d65ab6f78a` idx 7 | `obs_dfceeb53b2b53844` idx 7 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 22 | `run_7be9b5b129d0 0.2` | `obs_31bafab2df477f8e` idx 143 | `obs_d4407cc8f8ccf923` idx 143 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 23 | `run_7be9b5b129d0 1.3` | `obs_45bf1d987621d2b6` idx 6 | `obs_41e29394518bb03e` idx 6 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 24 | `run_7be9b5b129d0 2.0` (Historical) | `obs_61a3b783856bfb73` idx 201 | `obs_d4407cc8f8ccf923` idx 201 | **SUPPORTED** | no candidate | SUPPORTED | SUPPORTED | Historical label correct. Run 1: no candidate (evidence-selection defect in the harness: report-date annotation not stripped; fixed in 7304544). Runs 2-3 correct |
| 25 | `run_7be9b5b129d0 3.1` (Historical) | `obs_e84d1ea9eb494416` idx 228 | `obs_2702fdab1e0d6a49` idx 228 | **SUPPORTED** | no candidate | INSUFFICIENT_EVIDENCE | SUPPORTED | Historical label correct. Run 1: no candidate (harness annotation defect, fixed in 7304544). Run 2: INSUFFICIENT on the span alone, which lacks 'China' (evidence-selection defect, corrected by 773cb65). Run 3 correct |
| 26 | `run_fc1c9560577b 2.4` | `obs_e6096555d5c196fe` idx 210 | `obs_aa04a448cd214864` idx 210 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |
| 27 | `run_fc1c9560577b 3.7` | `obs_5c8e8e66f5748d64` idx 64 | `obs_27f45d20a516f753` idx 64 | **SUPPORTED** | SUPPORTED | SUPPORTED | SUPPORTED | agreement |

### B.2 Per-Claim evidence

#### 1. `run_ab1c836b61f4 0.4` — SUPPORTED

- **Claim as checked:** AWS reported $37.59 billion in Q1 2026, up 28% year over year.
- **Original citation:** `obs_da4f9a443c16a8fc` idx 126, Source “AWS vs Azure vs GCP Market Share Statistics 2026”
- **Verifier's recorded match:** `obs_95d548d9310502b7` idx 8 (check `web_refetch`, similarity 0.979)
  - The recorded match's text differs from the cited Passage. Cited: “AWS reported $37.59 billion in Q1 2026, up 28% year over year, beating analyst estimates by $950 million. AWS accounts for about 21% of Amazon’s total revenue.”. Recorded match: “AWS reported $37.59 billion in Q1 2026, up 28% versus 17% growth a year earlier.”.
- **Candidate [1] shown:** `obs_95d548d9310502b7` idx 8, sentences 0-0, similarity 0.9788, whole Passage shown (differs from the cited Passage's text): “AWS reported $37.59 billion in Q1 2026, up 28% versus 17% growth a year earlier.”
- **Candidate [2] shown:** `obs_95d548d9310502b7` idx 126, sentences 0-0, similarity 0.9676, whole Passage shown (equals the cited Passage's text): “AWS reported $37.59 billion in Q1 2026, up 28% year over year, beating analyst estimates by $950 million. AWS accounts for about 21% of Amazon’s total revenue.”
- **Material text:** “up 28% year over year”
- **Assessment:** Candidate 2 (idx 126) states the amount, the quarter and 'up 28% year over year' verbatim. Candidate 1 (idx 8, the verifier's recorded match) says 'up 28% versus 17% growth a year earlier', which gives the growth but not its base explicitly; candidate 2 alone suffices. Neither names the metric, and neither does the Claim.
- **Run 3 reply** (call 49, 545 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}, {"passage": 2, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 2. `run_ab1c836b61f4 0.6` — SUPPORTED

- **Claim as checked:** Synergy Research Group measured the wider cloud market growing 43% year over year in Q2 2026.
- **Original citation:** `obs_f1726d1a2036ebb4` idx 36, Source “Google Cloud Statistics: Market Share & Competition Report 2026 - TechnologyChecker.io”
- **Verifier's recorded match:** `obs_726ea7de504ee386` idx 36 (check `web_refetch`, similarity 0.865)
- **Candidate [1] shown:** `obs_726ea7de504ee386` idx 36, sentences 2-2, similarity 0.8651, whole Passage shown (equals the cited Passage's text): “Growth did not merely continue, it accelerated: 35.8% across full-year 2025, then 63%, then 82%. Google Cloud's first half of 2026 alone ($44.8 billion) brought in more than the whole of 2024 ($43.2 billion), and Q2 puts the segment at a $99 billion annual run rate. Synergy Research Group measured the wider market growing 43% year over year in Q2 2026, the fastest in eight years, which its chief analyst John Dinsdale attributed to AI demand having "lit a fire under the cloud market."”
- **Material text:** “Synergy Research Group measured the wider market growing 43% year over year in Q2 2026” / “"lit a fire under the cloud market."”
- **Assessment:** Synergy, 43%, year over year and Q2 2026 are stated. 'Wider cloud market': the Passage says 'the wider market' and, in the same sentence, attributes that growth to AI demand lighting 'a fire under the cloud market'. Borderline only in that 'cloud' comes from the quoted attribution, not the measurement clause; judged sufficient because it is in the same sentence of the shown Passage.
- **Run 3 reply** (call 50, 628 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 3. `run_ab1c836b61f4 1.5` — INSUFFICIENT_EVIDENCE

- **Claim as checked:** Synergy estimates that Microsoft held 20% of cloud infrastructure services in Q2 2026, unchanged from Q2 2025 and down from 23% in Q2 2024.
- **Original citation:** `obs_fa2a90a0de1c07d9` idx 348, Source “Cloud Market Share 2026: AWS vs Azure vs Google Cloud”
- **Verifier's recorded match:** `obs_64b457150e160b35` idx 348 (check `web_refetch`, similarity 0.900)
- **Candidate [1] shown:** `obs_64b457150e160b35` idx 348, sentences 0-0, similarity 0.9, whole Passage shown (equals the cited Passage's text): “Synergy estimates Microsoft held 20% in Q2 2026, unchanged from Q2 2025 and down from 23% in Q2 2024. Microsoft does not report standalone Azure revenue, so shares are estimates. It reported 43% growth in Azure and other cloud services for the quarter ended June 30, 2026.”
- **Material text:** “Synergy estimates Microsoft held 20% in Q2 2026, unchanged from Q2 2025 and down from 23% in Q2 2024.”
- **Assessment:** Stated: Synergy, Microsoft, 20%, Q2 2026, unchanged from Q2 2025, 23% in Q2 2024. Not stated: what the 20% is a share of. The Claim's 'of cloud infrastructure services' is a specific Synergy market definition; the Passage's only market wording, 'Azure and other cloud services', is Microsoft's own reporting line, a different measure. The definition is in idx 346, 10 and 38 of the same page, which F2 does not permit.
- **Run 3 reply** (call 51, 581 input tokens): `{"verdicts": [{"passage": 1, "verdict": "INSUFFICIENT_EVIDENCE", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 INSUFFICIENT_EVIDENCE
- **Classification:** Benchmark-label defect (historical VERIFIED). Runs 1-2: semantic judgment defect (permissive). Run 2 -> 3 change: unresolved between the added same-Passage sentences and model variability

#### 4. `run_ab1c836b61f4 1.6` — SUPPORTED

- **Claim as checked:** Holori's November 2025 market overview for 2026 put AWS at roughly 31 percent, Azure at 25 percent and Google Cloud at just 11 percent.
- **Original citation:** `obs_a5ab96d128964c3f` idx 55, Source “Google Cloud Hits 15% Share as AWS Falls to 28% [2026]”
- **Verifier's recorded match:** `obs_a309549bbb2ab2a8` idx 55 (check `web_refetch`, similarity 0.915)
- **Candidate [1] shown:** `obs_a309549bbb2ab2a8` idx 55, sentences 0-1, similarity 0.9145, whole Passage shown (equals the cited Passage's text): “None of this means Google Cloud is close to overtaking Azure, let alone AWS, in absolute share. A two-point year-on-year gain from a 13 percent base is meaningful momentum, not market leadership, and not every tracker agrees on the pace: Holori’s November 2025 market overview for 2026 put AWS at roughly 31 percent, Azure at 25 percent and Google Cloud at just 11 percent, a considerably wider gap than Synergy’s figures show. StatiSense’s own Q1 2026 ranking, published in August 2026, largely lines up with Synergy’s top-three numbers, AWS 28 percent, Azure 21 percent, Google Cloud 14 percent, but adds useful context further down the table: Oracle and Alibaba each held roughly 4 percent that quarter, a reminder of how much of the remaining market is fragmented among smaller challengers rather than consolidating around a fourth major player. But in a sector where the top three have controlled a remarkably stable combined share for years, any sustained movement between them is a signal worth watching, especially for enterprise architects deciding where to place new AI workloads in 2026 and 2027.”
- **Material text:** “Holori’s November 2025 market overview for 2026 put AWS at roughly 31 percent, Azure at 25 percent and Google Cloud at just 11 percent”
- **Assessment:** Stated verbatim: publisher, date, scope and the three shares.
- **Run 3 reply** (call 52, 848 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 5. `run_ab1c836b61f4 2.7` — SUPPORTED

- **Claim as checked:** Amazon expects to double its data center power capacity by the end of 2027 versus 2025, with capacity reservations for 2028 entering the mix.
- **Original citation:** `obs_0c72da74b73b4069` idx 38, Source “AI Capex to Hit $1 Trillion – And Estimates Are Still Too Low - I/O Fund”
- **Verifier's recorded match:** `obs_4387e7642fbbfcb0` idx 38 (check `web_refetch`, similarity 0.916)
- **Candidate [1] shown:** `obs_4387e7642fbbfcb0` idx 38, sentences 1-1, similarity 0.9158, whole Passage shown (equals the cited Passage's text): “Even after adding $20 billion to its capex outlook to account for rising memory costs, Amazon CEO Andy Jassy said AWS “will still not have enough capacity to meet all the demand we have in 2026, and I believe this dynamic will also be true in 2027 too.” Similar to Microsoft, Amazon also expects to double its data center power capacity by the end of 2027 versus 2025 as capacity reservations for 2028 enter the mix, implying capex intensity is likely to remain elevated as its data center footprint gets built out”
- **Material text:** “Amazon also expects to double its data center power capacity by the end of 2027 versus 2025 as capacity reservations for 2028 enter the mix”
- **Assessment:** Every element is stated; 'with ... entering the mix' restates 'as ... enter the mix'.
- **Run 3 reply** (call 53, 633 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 6. `run_ab1c836b61f4 2.9` — INSUFFICIENT_EVIDENCE

- **Claim as checked:** Alphabet's initial 2025 capex range of $71-73 billion was revised upward three times.
- **Original citation:** `obs_2a780a96eb8bc865` idx 21, Source “AI Capex 2026: The $690B Infrastructure Sprint - Futurum”
- **Verifier's recorded match:** `obs_650992fa4cac8aa3` idx 21 (check `web_refetch`, similarity 0.877)
- **Candidate [1] shown:** `obs_650992fa4cac8aa3` idx 21, sentences 0-0, similarity 0.877, whole Passage shown (equals the cited Passage's text): “Alphabet’s planned $175-185 billion is notable partly because it has already been revised three times upward from an initial $71-73 billion range for 2025. CEO Sundar Pichai acknowledged the scale is significant enough to cause concern internally, but pointed to a cloud backlog that surged 55% sequentially to over $240 billion. Alphabet also reported reducing Gemini serving costs by 78% over 2025 through model optimization, an important signal that efficiency gains are occurring alongside the spending increases.”
- **Material text:** “Alphabet’s planned $175-185 billion is notable partly because it has already been revised three times upward from an initial $71-73 billion range for 2025.”
- **Assessment:** Stated: Alphabet, an initial $71-73 billion range for 2025, revised upward three times. Not stated: that the figure is capex. Passage 21 says only 'planned' and, later, 'spending increases'. 'Capex' is in the section heading (idx 19, 'The Capex Landscape'), in idx 20 and 22 about other companies, and in the Source title, none of which F2 permits. Capital expenditure is a specific financial metric, and the Claim names it. This is the same class as 1.5 (a metric or denominator supplied by neighbouring context). Moderate confidence: a reader of the whole article would accept it.
- **Run 3 reply** (call 54, 611 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** Benchmark-label defect (historical VERIFIED). Runs 1-3: semantic judgment defect (permissive), consistent in all three runs

#### 7. `run_ab1c836b61f4 3.2` — SUPPORTED

- **Claim as checked:** In 2024 Google Cloud overtook Alphabet's subscriptions, platforms, and devices segment, becoming its second-largest business by revenue.
- **Original citation:** `obs_f1726d1a2036ebb4` idx 289, Source “Google Cloud Statistics: Market Share & Competition Report 2026 - TechnologyChecker.io”
- **Verifier's recorded match:** `obs_726ea7de504ee386` idx 289 (check `web_refetch`, similarity 0.873)
- **Candidate [1] shown:** `obs_726ea7de504ee386` idx 289, sentences 0-1, similarity 0.873, whole Passage shown (equals the cited Passage's text): “In 2024 Google Cloud overtook the "subscriptions, platforms, and devices" segment for the first time, making it Alphabet's second-largest business by revenue. It is worth noting that the Google Cloud segment as Alphabet reports it bundles Google Cloud Platform infrastructure with Google Workspace subscriptions, so it is not a like-for-like comparison with AWS, which is infrastructure only.”
- **Material text:** “In 2024 Google Cloud overtook the "subscriptions, platforms, and devices" segment for the first time, making it Alphabet's second-largest business by revenue.”
- **Assessment:** Stated verbatim.
- **Run 3 reply** (call 55, 598 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 8. `run_0b45a66fd66d 0.0` — SUPPORTED

- **Claim as checked:** Northwind Health revenue reached 612 million dollars in fiscal 2025.
- **Original citation:** `obs_5e2476030808cf39` idx 0, Source “northwind-health-annual-report-2025.pdf”
- **Verifier's recorded match:** `obs_5e2476030808cf39` idx 0 (check `document_passages`, similarity 0.916)
- **Candidate [1] shown:** `obs_5e2476030808cf39` idx 0, sentences 0-1, similarity 0.9156, whole Passage shown (equals the cited Passage's text): “Northwind Health revenue reached 612 million dollars in fiscal 2025. Revenue grew 14 percent compared with fiscal 2024, led by the cardiology segment. Operating margin improved to 11.5 percent in fiscal 2025.”
- **Material text:** “Northwind Health revenue reached 612 million dollars in fiscal 2025.”
- **Assessment:** Stated verbatim (uploaded document; check kind document_passages).
- **Run 3 reply** (call 56, 521 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 9. `run_0b45a66fd66d 0.1` — SUPPORTED

- **Claim as checked:** Northwind Health revenue grew 14 percent compared with fiscal 2024.
- **Original citation:** `obs_5e2476030808cf39` idx 0, Source “northwind-health-annual-report-2025.pdf”
- **Verifier's recorded match:** `obs_5e2476030808cf39` idx 0 (check `document_passages`, similarity 0.855)
- **Candidate [1] shown:** `obs_5e2476030808cf39` idx 0, sentences 0-1, similarity 0.8552, whole Passage shown (equals the cited Passage's text): “Northwind Health revenue reached 612 million dollars in fiscal 2025. Revenue grew 14 percent compared with fiscal 2024, led by the cardiology segment. Operating margin improved to 11.5 percent in fiscal 2025.”
- **Material text:** “Revenue grew 14 percent compared with fiscal 2024”
- **Assessment:** Stated; the preceding sentence of the same Passage names Northwind Health.
- **Run 3 reply** (call 57, 521 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 10. `run_0b45a66fd66d 0.3` — SUPPORTED

- **Claim as checked:** Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent.
- **Original citation:** `obs_5e2476030808cf39` idx 10, Source “northwind-health-annual-report-2025.pdf”
- **Verifier's recorded match:** `obs_5e2476030808cf39` idx 10 (check `document_passages`, similarity 1.000)
- **Candidate [1] shown:** `obs_5e2476030808cf39` idx 10, sentences 0-0, similarity 1.0, whole Passage shown (equals the cited Passage's text): “Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent.”
- **Material text:** “Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent.”
- **Assessment:** Stated verbatim.
- **Run 3 reply** (call 58, 475 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 11. `run_0b45a66fd66d 2.5` — SUPPORTED

- **Claim as checked:** Medtronic's FY2024 revenue was $32.4 billion, and its Q4 2024 revenue was $8.6 billion.
- **Original citation:** `obs_b9fe22278ef4eead` idx 297, Source “Medical Devices Market to Boost USD 1083.96 Billion by 2035”
- **Verifier's recorded match:** `obs_f70fec267f5837fe` idx 297 (check `web_refetch`, similarity 0.933)
- **Candidate [1] shown:** `obs_f70fec267f5837fe` idx 297, sentences 1-2, similarity 0.933, whole Passage shown (equals the cited Passage's text): “Medtronic plc is an American-Irish medical device company, offering groundbreaking technology solutions for the most complex and challenging conditions. The Q4 2024 revenue of Medtronic was $8.6 billion, while the FY2024 revenue was $32.4 billion. Out of $32.4 billion, $11.8 billion was from the cardiovascular sector and $9.4 billion was from the neuroscience sector.”
- **Material text:** “The Q4 2024 revenue of Medtronic was $8.6 billion, while the FY2024 revenue was $32.4 billion.”
- **Assessment:** Stated: both figures, periods and the company.
- **Run 3 reply** (call 59, 593 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 12. `run_f388cc91cf2a 0.1` — SUPPORTED

- **Claim as checked:** Global cumulative solar PV installed capacity reached 2,383 GW at end-2025.
- **Original citation:** `obs_887ba27ac7e4ef0e` idx 100, Source “Global Solar PV Installed Capacity 2025: 2,383 GW — IRENA Data | Solar Data Atlas”
- **Verifier's recorded match:** `obs_6359dd66fd93dce0` idx 1 (check `web_refetch`, similarity 0.864)
  - The recorded match's text differs from the cited Passage. Cited: “What is the total global solar PV installed capacity in 2025? According to IRENA 2026 H1, global solar PV installed capacity reached 2,383 GW at end-2025, up from 1,873 GW at end-2024. This represents a net addition of approximately 510 GW in a single year — a new record. Which country has the most solar PV capacity? China leads by a very wide margin with 1,200 GW (50.4% of global). The US is a di”. Recorded match: “World solar photovoltaic installed capacity reached 2,383 GW at end-2025, with 510 GW added in a single year. China accounts for 50.4% of global capacity. This page presents cumulative capacity trends, top 10 countries, Europe and USA focus, annual additions, and segment breakdown. All data from IRENA Renewable Capacity Statistics 2026, H1 edition.”.
- **Candidate [1] shown:** `obs_6359dd66fd93dce0` idx 1, sentences 0-1, similarity 0.8637, whole Passage shown (differs from the cited Passage's text): “World solar photovoltaic installed capacity reached 2,383 GW at end-2025, with 510 GW added in a single year. China accounts for 50.4% of global capacity. This page presents cumulative capacity trends, top 10 countries, Europe and USA focus, annual additions, and segment breakdown. All data from IRENA Renewable Capacity Statistics 2026, H1 edition.”
- **Candidate [2] shown:** `obs_6359dd66fd93dce0` idx 100, sentences 1-1, similarity 0.913, whole Passage shown (equals the cited Passage's text): “What is the total global solar PV installed capacity in 2025? According to IRENA 2026 H1, global solar PV installed capacity reached 2,383 GW at end-2025, up from 1,873 GW at end-2024. This represents a net addition of approximately 510 GW in a single year — a new record. Which country has the most solar PV capacity? China leads by a very wide margin with 1,200 GW (50.4% of global). The US is a distant second at 210 GW, followed by India (135 GW), Germany (106 GW), and Japan (92 GW). China has more capacity than the next 9 countries combined. How fast is solar PV growing globally? Extremely fast. Capacity grew 59-fold from 40 GW (2010) to 2,383 GW (2025). Annual additions went from 31 GW in 2011 to 510 GW in 2025. Capacity has roughly doubled every 2–3 years since 2018. How does solar compare to wind energy? Solar PV (2,383 GW) is 1.85 times larger than wind (1,291 GW) globally. Solar overtook wind around 2020 and the gap is growing. This is driven by faster cost reductions, easier deployment (especially distributed), and shorter construction timelines for solar. What is Europe’s total solar PV capacity? Europe had 405 GW at end-2025 (17% of global), with the EU-27 at 365 GW. Germany leads at 106 GW, followed by Spain (45 GW), Italy (41 GW), France (31 GW), Netherlands (26 GW), and Poland (25 GW). Europe’s growth has re-accelerated since 2020, adding 246 GW in 5 years.”
- **Candidate [3] shown:** `obs_6359dd66fd93dce0` idx 0, sentences 0-0, similarity 0.887, whole Passage shown (differs from the cited Passage's text): “Global Solar PV Installed Capacity 2025 — 2,383 GW”
- **Material text:** “global solar PV installed capacity reached 2,383 GW at end-2025, up from 1,873 GW at end-2024” / “Capacity grew 59-fold from 40 GW (2010) to 2,383 GW (2025)”
- **Assessment:** Global, solar PV, installed capacity, 2,383 GW and end-2025 are stated. 'Cumulative' adds no new fact: the Passage presents it as the installed stock, growing from 40 GW in 2010. Candidates 1 and 3 also state the figure.
- **Run 3 reply** (call 60, 1156 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}, {"passage": 2, "verdict": "SUPPORTED", "asserts_present_state": true}, {"passage": 3, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 13. `run_f388cc91cf2a 1.2` — SUPPORTED

- **Claim as checked:** Türkiye installed 4.7 GW of solar in 2025, according to a pv magazine International report dated 7 January 2026.
- **Original citation:** `obs_f5fa159c6b113664` idx 1276, Source “Solar power by country - Wikipedia”
- **Verifier's recorded match:** `obs_4e52c80f98d3abe9` idx 1276 (check `web_refetch`, similarity 0.893)
- **Candidate [1] shown:** `obs_4e52c80f98d3abe9` idx 1276, sentences 0-1, similarity 0.8933, whole Passage shown (equals the cited Passage's text): “↑ Jowett, Patrick (7 January 2026). "Türkiye installs 4.7 GW of solar in 2025". pv magazine International.”
- **Material text:** “Jowett, Patrick (7 January 2026). "Türkiye installs 4.7 GW of solar in 2025". pv magazine International.”
- **Assessment:** The evidence is a Wikipedia reference-list entry: the author, the date (7 January 2026), the publication (pv magazine International) and the headline 'Türkiye installs 4.7 GW of solar in 2025'. The Claim attributes the figure to that report ('according to'), which is exactly what the entry establishes. It does not establish the figure independently, and the Claim does not assert that it does. Weak evidence type, but sufficient for the attributed Claim.
- **Run 3 reply** (call 61, 504 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 14. `run_f388cc91cf2a 2.0` — SUPPORTED

- **Claim as checked:** The Global Solar Market Outlook analyses solar PV developments until 2025, with forward-looking scenarios up to 2030.
- **Original citation:** `obs_354f2f805330c523` idx 1, Source “Global Solar Council | Global Solar Market Outlook: 2026-2030”
- **Verifier's recorded match:** `obs_a41f771e79191b0d` idx 1 (check `web_refetch`, similarity 0.957)
- **Candidate [1] shown:** `obs_a41f771e79191b0d` idx 1, sentences 0-1, similarity 0.9568, whole Passage shown (equals the cited Passage's text): “The Global Solar Market Outlook provides an in-depth analysis of solar PV developments until 2025, with forward-looking scenarios up to 2030. As the sector enters a new phase shaped by policy shifts and system constraints, it delivers a detailed regional and country-level outlook, highlighting the key trends driving global markets. The report also sets out actionable policy recommendations and demonstrates how solar PV is becoming a cornerstone of energy security. In addition, it features a dedicated chapter analysing the Australian solar PV market.”
- **Material text:** “The Global Solar Market Outlook provides an in-depth analysis of solar PV developments until 2025, with forward-looking scenarios up to 2030.”
- **Assessment:** Stated verbatim.
- **Run 3 reply** (call 62, 628 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 15. `run_06105d324454 1.1` — SUPPORTED

- **Claim as checked:** BYD revised its 2025 sales target downward to about 4.6 million units from an earlier, more aggressive target.
- **Original citation:** `obs_e57ad6fb7bd81240` idx 21, Source “Tesla vs BYD & the Global EV Price War: How Low Can Tesla Go? – TESMAG”
- **Verifier's recorded match:** `obs_fef9895d6a3756dc` idx 21 (check `web_refetch`, similarity 0.924)
- **Candidate [1] shown:** `obs_fef9895d6a3756dc` idx 21, sentences 0-0, similarity 0.9241, whole Passage shown (equals the cited Passage's text): “In 2025, BYD revised its sales target downward (from an earlier aggressive target) to about 4.6 million units, possibly acknowledging margin stress or market saturation.”
- **Material text:** “In 2025, BYD revised its sales target downward (from an earlier aggressive target) to about 4.6 million units”
- **Assessment:** Stated.
- **Run 3 reply** (call 63, 504 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 16. `run_06105d324454 1.7` — SUPPORTED

- **Claim as checked:** Tesla has already cut prices in China.
- **Original citation:** `obs_e57ad6fb7bd81240` idx 85, Source “Tesla vs BYD & the Global EV Price War: How Low Can Tesla Go? – TESMAG”
- **Verifier's recorded match:** `obs_fef9895d6a3756dc` idx 85 (check `web_refetch`, similarity 0.853)
- **Candidate [1] shown:** `obs_fef9895d6a3756dc` idx 85, sentences 0-0, similarity 0.8527, whole Passage shown (equals the cited Passage's text): “Tesla has already cut prices in China; reports suggest Tesla is considering a lower-cost Model Y variant (“E41”) by 2026 to better compete. (NY Post report)”
- **Material text:** “Tesla has already cut prices in China”
- **Assessment:** Stated verbatim.
- **Run 3 reply** (call 64, 484 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 17. `run_06105d324454 2.6` — SUPPORTED

- **Claim as checked:** The LFP battery industry faces constraints including lithium availability, supply chain constraints, and the initial investment required to set up manufacturing facilities.
- **Original citation:** `obs_b647a21595562e09` idx 110, Source “Lithium Iron Phosphate Battery Market Size, Growth Report, 2026-2033”
- **Verifier's recorded match:** `obs_d74c1df2ec8da16a` idx 110 (check `web_refetch`, similarity 0.857)
- **Candidate [1] shown:** `obs_d74c1df2ec8da16a` idx 110, sentences 3-3, similarity 0.857, whole Passage shown (equals the cited Passage's text): “Despite strong growth, the LFP battery industry faces challenges, including lower energy density than other lithium-ion batteries such as NMC and NCA. This limitation can affect performance in applications requiring long driving ranges or compact battery sizes. Additionally, intense competition from alternative battery chemistries and rapid technological advancements may impact market positioning. Supply chain constraints related to lithium availability and the initial investment required to set up manufacturing facilities can also act as barriers to market growth.”
- **Material text:** “the LFP battery industry faces challenges” / “Supply chain constraints related to lithium availability and the initial investment required to set up manufacturing facilities can also act as barriers to market growth.”
- **Assessment:** Subject (the LFP battery industry 'faces challenges') and all three items are stated in the shown Passage; 'also' places the supply-chain sentence in that list of challenges. Minor modality softening ('can also act as barriers' vs 'faces constraints') is judged not material.
- **Run 3 reply** (call 65, 618 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 INSUFFICIENT_EVIDENCE; run 3 SUPPORTED
- **Classification:** Historical label correct. Run 1: SUPPORTED on the span alone, which lacks the LFP subject (semantic judgment defect, permissive, older instruction). Run 2: INSUFFICIENT on the span alone (evidence-selection defect: span without its Passage's subject), corrected by 773cb65; run 3 correct

#### 18. `run_06105d324454 3.0` — SUPPORTED

- **Claim as checked:** The China All-Solid-State Battery Collaborative Innovation Platform (CASIP) was formed in January 2026, uniting competitors including CATL and BYD.
- **Original citation:** `obs_77f645d65ab6f78a` idx 6, Source “CATL Energy Storage 2026, 150 kWh Nio Partnership - ENKI”
- **Verifier's recorded match:** `obs_dfceeb53b2b53844` idx 6 (check `web_refetch`, similarity 0.944)
- **Candidate [1] shown:** `obs_dfceeb53b2b53844` idx 6, sentences 0-1, similarity 0.9437, whole Passage shown (equals the cited Passage's text): “The strategic inflection point occurred in January 2026 with the formation of the China All-Solid-State Battery Collaborative Innovation Platform (CASIP). This state-backed consortium united fierce competitors like CATL and BYD into a national team, signaling a transition from internal competition to a unified push for global market capture.”
- **Candidate [2] shown:** `obs_dfceeb53b2b53844` idx 25, sentences 0-1, similarity 0.9315, whole Passage shown (differs from the cited Passage's text): “The January 2026 formation of the China All-Solid-State Battery Collaborative Innovation Platform (CASIP) marks a pivotal strategic consolidation, transforming the domestic landscape from one of fierce competition to a unified national front. This alliance pools China’s immense resources in battery manufacturing and automotive engineering to accelerate technology standardization and supply chain development, aiming to create an insurmountable lead over international rivals.”
- **Candidate [3] shown:** `obs_dfceeb53b2b53844` idx 24, sentences 0-0, similarity 0.8516, whole Passage shown (differs from the cited Passage's text): “CATL’s CASIP Alliance with BYD and Nio for Solid-State Batteries (2026)”
- **Material text:** “The strategic inflection point occurred in January 2026 with the formation of the China All-Solid-State Battery Collaborative Innovation Platform (CASIP). This state-backed consortium united fierce competitors like CATL and BYD”
- **Assessment:** Candidate 1 states the name, January 2026, and that it united competitors including CATL and BYD. Candidate 2 (no CATL or BYD) and candidate 3 (a heading) do not; run 3 judged them INSUFFICIENT, which is correct.
- **Run 3 reply** (call 66, 753 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}, {"passage": 2, "verdict": "INSUFFICIENT_EVIDENCE", "asserts_present_state": true}, {"passage": 3, "verdict": "INSUFFICIENT_EVIDENCE", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 19. `run_06105d324454 3.1` — SUPPORTED

- **Claim as checked:** Solid-state battery technology is moving from laboratory (TRL 4-7) toward small-scale commercial production (TRL 8), primarily driven by Chinese firms targeting initial output in 2026.
- **Original citation:** `obs_77f645d65ab6f78a` idx 53, Source “CATL Energy Storage 2026, 150 kWh Nio Partnership - ENKI”
- **Verifier's recorded match:** `obs_dfceeb53b2b53844` idx 53 (check `web_refetch`, similarity 0.885)
- **Candidate [1] shown:** `obs_dfceeb53b2b53844` idx 53, sentences 0-1, similarity 0.8853, whole Passage shown (equals the cited Passage's text): “Solid-state battery technology is making a rapid, state-accelerated transition from the laboratory (Technology Readiness Level 4-7) directly toward small-scale commercial production (TRL 8). This jump, primarily driven by Chinese firms targeting initial output in 2026, bypasses the more incremental development cycles seen in other regions but hinges on solving critical manufacturing cost and material supply chain hurdles.”
- **Material text:** “Solid-state battery technology is making a rapid, state-accelerated transition from the laboratory (Technology Readiness Level 4-7) directly toward small-scale commercial production (TRL 8). This jump, primarily driven by Chinese firms targeting initial output in 2026”
- **Assessment:** Stated.
- **Run 3 reply** (call 67, 614 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 20. `run_06105d324454 3.3` — INSUFFICIENT_EVIDENCE

- **Claim as checked:** BYD is targeting 400 Wh/kg with 5C charging for its solid-state battery, with initial production in 2027.
- **Original citation:** `obs_d31bafe53dc36b95` idx 44, Source “Solid-State Batteries 2026: How the Technology Is Finally Reaching Commercial Use”
- **Verifier's recorded match:** `obs_a61d15cce53c78b8` idx 44 (check `web_refetch`, similarity 0.892)
- **Candidate [1] shown:** `obs_a61d15cce53c78b8` idx 44, sentences 0-0, similarity 0.8923, whole Passage shown (equals the cited Passage's text): “BYD: Targeting 400 Wh/kg with 5C charging, initial production 2027”
- **Material text:** “BYD: Targeting 400 Wh/kg with 5C charging, initial production 2027”
- **Assessment:** Stated: BYD, 400 Wh/kg, 5C charging, initial production 2027. Not stated: that this is BYD's solid-state battery. The Passage is a single list item. 'Solid-state' is in the Source title and the page's surrounding section (idx 42, 'Leading manufacturers are hedging their bets:'), neither permitted. The same page's idx 57 places 400 Wh/kg at the 'hybrid' stage of China's roadmap ('500 Wh/kg true solid-state'), so the qualifier is not trivially implied either.
- **Run 3 reply** (call 68, 481 input tokens): `{"verdicts": [{"passage": 1, "verdict": "INSUFFICIENT_EVIDENCE", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 INSUFFICIENT_EVIDENCE; run 3 INSUFFICIENT_EVIDENCE
- **Classification:** Benchmark-label defect (historical VERIFIED; already adjudicated in stage_b1_verified_adjudication.json). Run 1: semantic judgment defect (permissive, older instruction). Runs 2-3 correct on the identical request

#### 21. `run_06105d324454 3.6` — SUPPORTED

- **Claim as checked:** GAC Group plans to begin mass production of all-solid-state batteries in 2026.
- **Original citation:** `obs_77f645d65ab6f78a` idx 7, Source “CATL Energy Storage 2026, 150 kWh Nio Partnership - ENKI”
- **Verifier's recorded match:** `obs_dfceeb53b2b53844` idx 7 (check `web_refetch`, similarity 0.878)
- **Candidate [1] shown:** `obs_dfceeb53b2b53844` idx 7, sentences 1-1, similarity 0.8775, whole Passage shown (equals the cited Passage's text): “Following this consolidation, commercialization targets became more aggressive and explicit. GAC Group announced plans for mass production of all-solid-state batteries to begin in 2026, while Dongfeng Motor is targeting a 2026 launch for solid-state EVs with a projected range of over 620 miles.”
- **Material text:** “GAC Group announced plans for mass production of all-solid-state batteries to begin in 2026”
- **Assessment:** Stated.
- **Run 3 reply** (call 69, 537 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 22. `run_7be9b5b129d0 0.2` — SUPPORTED

- **Claim as checked:** BYD sold 1,108,048 NEVs in Q2 2026, which was 58.2% more than in Q1 2026 and 3.2% below Q2 2025.
- **Original citation:** `obs_31bafab2df477f8e` idx 143, Source “BYD Sales by Model and Country Statistics (Jul 2026) - Tridens”
- **Verifier's recorded match:** `obs_d4407cc8f8ccf923` idx 143 (check `web_refetch`, similarity 0.883)
- **Candidate [1] shown:** `obs_d4407cc8f8ccf923` idx 143, sentences 0-1, similarity 0.8826, whole Passage shown (equals the cited Passage's text): “Q2 2026 brought a strong rebound. BYD sold 1,108,048 NEVs, 58.2% more than in Q1, though still 3.2% below Q2 2025.”
- **Material text:** “Q2 2026 brought a strong rebound. BYD sold 1,108,048 NEVs, 58.2% more than in Q1, though still 3.2% below Q2 2025.”
- **Assessment:** All figures are stated; Q2 2026 is named in the preceding sentence of the same Passage, and 'Q1' within that sentence is Q1 of the same year.
- **Run 3 reply** (call 70, 526 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 23. `run_7be9b5b129d0 1.3` — SUPPORTED

- **Claim as checked:** Even at Tesla's new price point, BYD's mass-market vehicles remain more than $10,000 cheaper than Tesla in several markets, according to industry analysts.
- **Original citation:** `obs_45bf1d987621d2b6` idx 6, Source “Tesla’s Model Y just got cheaper, but BYD is still miTesla Model Y price cut fails to beat BYD in global EV price warles ahead - Rest of WorldRest of World”
- **Verifier's recorded match:** `obs_41e29394518bb03e` idx 6 (check `web_refetch`, similarity 0.990)
- **Candidate [1] shown:** `obs_41e29394518bb03e` idx 6, sentences 0-0, similarity 0.9904, whole Passage shown (equals the cited Passage's text): “Even at the new price point, however, BYD’s mass-market vehicles remain more than $10,000 cheaper than Tesla in several markets, according to industry analysts.”
- **Material text:** “Even at the new price point, however, BYD’s mass-market vehicles remain more than $10,000 cheaper than Tesla in several markets, according to industry analysts.”
- **Assessment:** Stated, apart from whose price point it is. The Passage does not say 'Tesla's', but its own logic fixes it: BYD's vehicles 'remain' cheaper 'than Tesla' 'even at the new price point', so the price that changed is Tesla's. Borderline entity attribution established within the sentence; idx 4-5 (not permitted) confirm it.
- **Run 3 reply** (call 71, 520 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 24. `run_7be9b5b129d0 2.0` — SUPPORTED

- **Claim as checked:** The earlier report found that BYD's 2025 sales reached 4,602,436 vehicles, with nearly half fully electric.
- **Claim as drafted:** The earlier report found that BYD's 2025 sales reached 4,602,436 vehicles, with nearly half fully electric (reported 2026-10-19). (Historical; the report-date annotation is removed before checking, as production does)
- **Original citation:** `obs_61a3b783856bfb73` idx 201, Source “BYD Sales by Model and Country Statistics (Jul 2026) - Tridens”
- **Verifier's recorded match:** `obs_d4407cc8f8ccf923` idx 201 (check `historical_web_refetch`, similarity 0.970)
- **Candidate [1] shown:** `obs_d4407cc8f8ccf923` idx 201, sentences 0-0, similarity 0.9696, whole Passage shown (equals the cited Passage's text): “BYD’s 2025 sales reached 4,602,436 vehicles, with nearly half fully electric.”
- **Material text:** “BYD’s 2025 sales reached 4,602,436 vehicles, with nearly half fully electric.”
- **Assessment:** The finding is stated verbatim. The framing 'The earlier report found that' asserts the earlier Report's finding, which the instruction says to judge as the finding; the Passage cannot show what the earlier Report said, so only the finding is audited.
- **Run 3 reply** (call 72, 487 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 no candidate; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** Historical label correct. Run 1: no candidate (evidence-selection defect in the harness: report-date annotation not stripped; fixed in 7304544). Runs 2-3 correct

#### 25. `run_7be9b5b129d0 3.1` — SUPPORTED

- **Claim as checked:** The earlier report found that CATL and BYD produce LFP batteries at large scale in China, and that large production helps reduce battery costs.
- **Claim as drafted:** The earlier report found that CATL and BYD produce LFP batteries at large scale in China, and that large production helps reduce battery costs (reported 2026-10-19). (Historical; the report-date annotation is removed before checking, as production does)
- **Original citation:** `obs_e84d1ea9eb494416` idx 228, Source “Lithium Iron Phosphate Battery Market Size & Growth 2026–2034”
- **Verifier's recorded match:** `obs_2702fdab1e0d6a49` idx 228 (check `historical_web_refetch`, similarity 0.886)
- **Candidate [1] shown:** `obs_2702fdab1e0d6a49` idx 228, sentences 2-3, similarity 0.8863, whole Passage shown (equals the cited Passage's text): “China dominated the regional market with a 45.8% share in 2025. The country has a strong battery manufacturing industry and a large EV market. Major companies such as CATL and BYD produce LFP batteries at a large scale. Large production helps reduce battery costs. China is also exporting more batteries and electric vehicles to global markets. Growing EV sales and energy storage projects are further increasing demand. A strong supply chain further supports faster production. These factors fuel LiFePO4 battery manufacturing in China, thereby driving the LFP battery China market growth.”
- **Material text:** “China dominated the regional market with a 45.8% share in 2025. The country has a strong battery manufacturing industry” / “Major companies such as CATL and BYD produce LFP batteries at a large scale. Large production helps reduce battery costs.” / “These factors fuel LiFePO4 battery manufacturing in China”
- **Assessment:** Large-scale production by CATL and BYD and its cost effect are stated verbatim. 'In China' is established by the same Passage, which presents these companies as factors that 'fuel LiFePO4 battery manufacturing in China'. Framing as for 2.0.
- **Run 3 reply** (call 73, 634 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": true}]}`
- **Outcomes:** run 1 no candidate; run 2 INSUFFICIENT_EVIDENCE; run 3 SUPPORTED
- **Classification:** Historical label correct. Run 1: no candidate (harness annotation defect, fixed in 7304544). Run 2: INSUFFICIENT on the span alone, which lacks 'China' (evidence-selection defect, corrected by 773cb65). Run 3 correct

#### 26. `run_fc1c9560577b 2.4` — SUPPORTED

- **Claim as checked:** For the first quarter of 2026, AMD expects revenue of approximately $9.8 billion, plus or minus $300 million, including approximately $100 million of AMD Instinct MI308 sales to China.
- **Original citation:** `obs_e6096555d5c196fe` idx 210, Source “AMD Reports Fourth Quarter and Full Year 2025 Financial Results :: Advanced Micro Devices, Inc. (AMD)”
- **Verifier's recorded match:** `obs_aa04a448cd214864` idx 210 (check `web_refetch`, similarity 0.958)
- **Candidate [1] shown:** `obs_aa04a448cd214864` idx 210, sentences 0-1, similarity 0.9577, whole Passage shown (equals the cited Passage's text): “For the first quarter of 2026, AMD expects revenue to be approximately $9.8 billion, plus or minus $300 million, including approximately $100 million of AMD Instinct MI308 sales to China. The mid-point of the revenue range represents year-over-year growth of approximately 32% and a sequential decline of approximately 5%. Non-GAAP gross margin is expected to be approximately 55%.”
- **Material text:** “For the first quarter of 2026, AMD expects revenue to be approximately $9.8 billion, plus or minus $300 million, including approximately $100 million of AMD Instinct MI308 sales to China.”
- **Assessment:** Stated verbatim.
- **Run 3 reply** (call 74, 609 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree

#### 27. `run_fc1c9560577b 3.7` — SUPPORTED

- **Claim as checked:** NVIDIA is expected to price the B30A at about half the cost of a B300.
- **Original citation:** `obs_5c8e8e66f5748d64` idx 64, Source “Should the US Sell Blackwell Chips to China? | IFP”
- **Verifier's recorded match:** `obs_27f45d20a516f753` idx 64 (check `web_refetch`, similarity 0.864)
- **Candidate [1] shown:** `obs_27f45d20a516f753` idx 64, sentences 0-1, similarity 0.8636, whole Passage shown (equals the cited Passage's text): “To compensate for the reduced performance, NVIDIA is expected to price the B30A at about half the cost of a B300 — a move Chinese tech companies have privately called a “good deal.” This is because the most relevant measure of AI computing capabilities is not the performance of individual chips, but the performance of large clusters of those chips networked together in data centers. By networking B30As together, China can access computing capabilities similar to those of US AI labs at a similar price. We estimate that to match the capabilities of US labs using a B300-based AI training cluster, Chinese labs would need to spend approximately 20% more when using B30As.10”
- **Material text:** “NVIDIA is expected to price the B30A at about half the cost of a B300”
- **Assessment:** Stated verbatim.
- **Run 3 reply** (call 75, 635 input tokens): `{"verdicts": [{"passage": 1, "verdict": "SUPPORTED", "asserts_present_state": false}]}`
- **Outcomes:** run 1 SUPPORTED; run 2 SUPPORTED; run 3 SUPPORTED
- **Classification:** None: audit, historical label and all judged runs agree


## C. Detailed analysis

### C.1 Claim `run_ab1c836b61f4 1.5` (the run 3 failure)

- **Shown evidence (run 3):** the whole of `obs_64b457150e160b35` idx 348:
  - “Synergy estimates Microsoft held 20% in Q2 2026, unchanged from Q2 2025 and down from 23% in Q2 2024.”
  - “Microsoft does not report standalone Azure revenue, so shares are estimates.”
  - “It reported 43% growth in Azure and other cloud services for the quarter ended June 30, 2026.”
- **What is missing:** the Claim says the 20% is a share “of cloud infrastructure services”. The Passage never says what the share is of.
  - Its only market wording, “Azure and other cloud services”, is Microsoft's own reporting line.
  - The same page itself warns that measures differ. Idx 346 contrasts Synergy's cloud infrastructure services with Gartner's “narrower public IaaS measure”, and idx 360 says Microsoft's cloud revenue lines “are not like for like”.
  - The denominator is therefore material.
- **No single Passage supports the whole Claim.** I searched all 440 Passages of the Observation:
  - Idx 10 and 38 state Microsoft's 20% of “worldwide cloud infrastructure services” in Q2 2026, but not the Q2 2025 or Q2 2024 comparisons.
  - Idx 129 says “unchanged from a year earlier” without the market.
  - The figure check removes idx 10 and 38 because they lack 23%, 2024 and 2025, and even if shown, each would support only part of the Claim.
  - So the Claim fuses two or more Passages while citing one. This is not an evidence-selection defect under the single-Passage F2 policy.
- **Label:** INSUFFICIENT_EVIDENCE, high confidence.
- **What each run saw and answered:**

| Run | Instruction | Evidence shown | Input tokens | Reply |
|---|---|---|---|---|
| 1 | Original (`e517b28`) | Sentence 1 only | 331 | SUPPORTED, present state |
| 2 | Stricter (`7304544`) | Sentence 1 only | 522 | SUPPORTED, present state |
| 3 | Stricter, unchanged | Whole Passage (3 sentences) | 581 | INSUFFICIENT_EVIDENCE, present state (757 output tokens, unusually long) |

  Runs 1 and 2 were shown the matched span only. That was the behaviour before `773cb65`, and the input-token difference between runs 2 and 3 (59) matches the two added sentences.
- **Classification:**
  - The historical label is a **benchmark-label defect**.
  - Runs 1 and 2 are a **semantic judgment defect (permissive)**: sentence 1 alone lacks the market as well, so they accepted the same gap.
  - Run 3 is **correct**.
  - Whether run 3 rejected the Claim *because* the added sentences put a different measure in front of it (“Azure and other cloud services”), or through sampling variability, is **unresolved**. There is one sample per run, and the requests differ.

### C.2 Claim `run_06105d324454 3.3` (reconfirmed)

- **Shown evidence (runs 2 and 3, identical requests, 481 input tokens):** the whole of `obs_a61d15cce53c78b8` idx 44, which is a single list item: “BYD: Targeting 400 Wh/kg with 5C charging, initial production 2027”.
- **What is missing:** the Claim's “for its solid-state battery”.
  - “Solid-state” is in the Source title and in the page's surrounding section (idx 42, “Leading manufacturers are hedging their bets:”), neither of which is permitted.
  - The qualifier is not trivially implied either. Idx 57 of the same page puts 400 Wh/kg at the “hybrid” stage of China's roadmap, with “500 Wh/kg true solid-state by 2035”.
  - No other Passage of the Observation states BYD, 400 Wh/kg and solid-state together.
- **Label:** INSUFFICIENT_EVIDENCE, high confidence, under the same standard as the other 26 Claims.
- **What each run did:**
  - Run 1 (original instruction) judged it SUPPORTED: a **semantic judgment defect (permissive)**.
  - Runs 2 and 3 judged it INSUFFICIENT_EVIDENCE on the byte-identical request, which is correct.
- **Classification:** the historical label is a **benchmark-label defect**. It is already recorded in `stage_b1_verified_adjudication.json`.

### C.3 Claim `run_ab1c836b61f4 2.9` (newly questioned; run 3 response judged incorrect)

- **Claim:** “Alphabet's initial 2025 capex range of $71-73 billion was revised upward three times.”
- **Shown evidence:** the whole of `obs_650992fa4cac8aa3` idx 21:
  - “Alphabet's planned $175-185 billion is notable partly because it has already been revised three times upward from an initial $71-73 billion range for 2025…”
  - The Passage ends with “…efficiency gains are occurring alongside the spending increases.”
- **What is missing:** the Passage never says the figure is capital expenditure.
  - “Capex” appears in the section heading (idx 19, “The Capex Landscape: Who is Spending What”), in idx 20 and 22 (about Amazon, Microsoft and Meta), and in the Source title. None of these is permitted.
  - No Passage of the Observation states Alphabet, capex and $71-73 billion together: “71” occurs only in idx 21.
  - Capex is a specific financial metric, and the Claim names it, so this is the same class as `1.5`.
- **Label:** INSUFFICIENT_EVIDENCE, **moderate confidence**. This is the closest call in the audit: a reader of the whole article would accept it, but F2 judges one Passage.
- **What each run did:** runs 1, 2 and 3 all judged it SUPPORTED (inputs 300, 491 and 611 tokens), so this is not variability.
- **Classification:**
  - The historical label is a **benchmark-label defect**.
  - All three runs are a **semantic judgment defect (permissive)**.
- **Why B-1 did not catch it:** the historical label agreed with the run outcome, so the case scored as a pass.

### C.4 Borderline SUPPORTED labels (recorded for review, not disputed)

- **`0.6`:** “wider cloud market”. “Cloud” comes from the analyst's attribution (“lit a fire under the cloud market”) in the same sentence, not from the measurement clause.
- **`1.3`:** “Tesla's new price point”. Whose price point it is follows from the sentence's own logic (“remain … cheaper than Tesla, even at the new price point”); the Passage does not name Tesla as the owner.
- **`1.2`:** the evidence is a Wikipedia reference-list entry, giving the author, date, publication and headline. It supports the Claim only because the Claim is attributed (“according to a pv magazine International report dated 7 January 2026”).
- **`2.6`:** the Passage says “can also act as barriers”, while the Claim says the industry “faces constraints”. I judged this modality difference not material because the Passage introduces the list with “the LFP battery industry faces challenges”.
- **Historical `2.0` and `3.1`:** the framing “The earlier report found that” cannot be checked against the Passage. Only the finding was audited, as the instruction directs.

If GPT-6 applies a stricter reading than mine to `0.6` or `1.3`, they would move to INSUFFICIENT_EVIDENCE in the same way as `2.9`, and run 3, which judged both SUPPORTED, would then be permissive on them too.

### C.5 Run 3 responses judged semantically incorrect

- **Incorrect:** only `2.9` (SUPPORTED where the audit says INSUFFICIENT_EVIDENCE).
- **Correct non-SUPPORTED judgments:** run 3's other non-SUPPORTED judgments on verified Claims all match the audit:
  - `1.5` and `3.3`: INSUFFICIENT_EVIDENCE;
  - `3.0` candidates 2 and 3: INSUFFICIENT_EVIDENCE. Candidate 2 lacks CATL and BYD, and candidate 3 is a heading. Candidate 1 was SUPPORTED, so the Claim was verified.
- **`asserts_present_state`:** the flag looks unreliable (for example, `true` for the IRENA end-2025 figure and the 2.9 revision history). The Fact-Checker does not use it, so it has no effect on B-1. Only Synthesis reads it (`synthesize.py:495`). I noted it but did not investigate further.

## D. Root-cause summary

### Confirmed by the evidence
1. **Incorrect historical labels (primary cause of the formal failure).**
   - 3 of the 27 VERIFIED labels do not meet the F2 contract.
   - T18 produced them under the original, looser instruction and showed the judge the matched span alone. Run 1, which reproduces those conditions, supported all three.
2. **Passage-level context limitation (now fixed).**
   - In run 2, `2.6` and Historical `3.1` failed because the span lacked its Passage's subject or place.
   - With the same-Passage context added in `773cb65`, both are correctly SUPPORTED in run 3.
   - No audited Claim needed context from outside its own Passage to be correctly SUPPORTED.
3. **Permissive entailment on unstated qualifiers.**
   - Under the current instruction, the judge accepted an unstated market or metric qualifier in 3 of 6 judgments in this class.
   - `2.9` was accepted in all three runs, so this is a reproducible miss, not noise.
   - The instruction already requires that “the passage states everything the statement asserts”, so the judge is not consistently following it in this class.
   - The 40 adversarial negatives, which include a “wrong measure” case (N02, rejected), do not include an *added unstated qualifier*, which is why B-1's negatives did not reveal this.
4. **Harness evidence-selection defect in run 1 (fixed).** Historical `2.0` and `3.1` were never judged because the report-date annotation was not stripped. This was fixed in `7304544`.

### Not supported by the evidence
- **Candidate retrieval or ranking defect:**
  - For every Claim, the best permitted evidence was among the candidates shown.
  - For `1.5`, `2.9` and `3.3`, no single Passage of the matched Observation states the whole Claim.
  - No candidate was dropped by the request cap: the largest request is 759 estimated tokens against the 3,000-token cap.
- **Overly strict entailment:** run 3 rejected no Claim that the audit labels SUPPORTED.

### Hypotheses (not established)
- **Where the qualifiers come from:** the drafting step, which sees neighbouring Passages and headings, may attach their context to a Claim that cites one Passage. All three defective Claims fit this pattern (a neighbouring Passage for `1.5`, a section heading for `2.9`, the title or section for `3.3`). This would be a Synthesis or drafting matter, not F2. The records here cannot confirm it.
- **Why `1.5` flipped between runs 2 and 3:** the extra sentences in run 3 put a competing measure (“Azure and other cloud services”) next to the share, which may have prompted the rejection. Alternatively, it is sampling variability. One sample per run cannot separate the two.
- **Whether the judge follows the instruction:** it may treat a qualifier as “stated” when it is plausible for the Passage's topic (capex, in a spending context), which would explain `2.9`'s consistent acceptance.

## E. Recommendation

**Recommended: Option 1.** The current fact-checker is adequate for B-1, subject to documented historical benchmark corrections and an explicitly approved gate disposition. I have not implemented any of this.

### Why Option 1
- The sole formal failure (`1.5`) is a correct verdict against an incorrect label.
- Run 3 matches the audit on 26 of 27 verified Claims. It also has 0 of 40 negatives SUPPORTED, all positives SUPPORTED, and no unreadable replies.
- The context change of `773cb65` did what it was meant to do, with no observed over-strictness.

### What the owner and GPT-6 would need to decide (not done)
1. **Correct the benchmark:**
   - whether to adjudicate `1.5` and `2.9` as INSUFFICIENT_EVIDENCE, as `3.3` already is;
   - how to treat the borderline `0.6` and `1.3`.
2. **Dispose of the `2.9` miss explicitly.** With all three corrections, run 3 still differs from the benchmark on `2.9`, a false SUPPORTED. Under the existing `b1_decision` structure, which requires the exact expected verdict for an adjudicated Claim, a corrected benchmark would still FAIL run 3. The disposition must therefore choose between:
   - accepting the unstated-qualifier weakness as a documented residual risk, which is low in impact because each leaked Claim is true according to its own Source, just not according to the cited Passage alone; or
   - treating it as Option 2.

### The case for Option 2
The `2.9` miss is reproducible (3 of 3 runs), and the class leaks about half the time under the current instruction. That is a real precision gap against F2's own contract.

I do not recommend making it a blocker now, for three reasons:
- A scoped correction would be an instruction change, for example requiring that the market, metric or denominator of any figure is stated in the Passage. That needs new paid calibration, including negatives for this class.
- It risks over-strictness on the 24 correctly supported Claims.
- Its benefit is attribution precision, not catching false facts.

If GPT-6 judges attribution precision to be part of the F2 contract that B-1 must prove, Option 2 is the consistent choice. Its scope would be a single instruction clause plus added unstated-qualifier negatives.

### Why not Option 3
The question asked is answerable on this evidence. What remains uncertain (the cause of the `1.5` flip, and the leak rate on more than three cases) does not change which way the formal failure points.

## F. Verification and preservation

| Item | SHA-256 | Status |
|---|---|---|
| Saved evaluation DB `eval.db` | `d061f8b123c4702d6463d054bf0f2144687f02bc3b4199734c43da488ccf9aff` | Unchanged; opened read-only (`mode=ro`) throughout |
| B-1 run 1 record | `2157c0eb18044bb7168d6aa9d98290030da5cb092532b710b7ee9ccd992704ad` | Unchanged |
| B-1 run 2 record | `848b4f686399e3b41758f541ecad4c210d08b1014a38e6eea2f62f88755d4a3b` | Unchanged |
| B-1 run 3 record | `7b2bd1d8fdaadcefd8a4dd231d6b5f82e009e22fff3e2c27ad1e267aa278cfd3` | Unchanged |
| `tests/fixtures/t18/stage_b1_cases.json` | LF-normalised `704bbe19215dafb90f7e452d109b8c066cae03b94612175fc72455d8ad217b72` (as pinned) | Unchanged |
| `tests/fixtures/t18/stage_b1_verified_adjudication.json` | `aa385b161031ff086a732ef6d26331caf385924f18bdd148000a58b87afa12b6` (working-tree bytes) | Unchanged; no diff against `bac1a11` |

- **Paid calls:** **zero**. No model, inference or search request was made. Embeddings were computed locally with FastEmbed (`BAAI/bge-small-en-v1.5`), as the offline integration tests do.
- **Repository files modified:** **none**. `git status` is clean at `bac1a11`. No commits, pushes, PRs or merges.
- **Audit artefacts** are all in `C:\Users\subha\AppData\Local\Temp\claude\C--Code-CodeBasics-Assignments-insightforge-agent\3eda3f50-0d4b-4279-90e1-aee76f61de76\scratchpad\b1-claim-audit\`:
  - `dump_27.py` and `claims_27.json`: the offline reconstruction;
  - `claims_27_readable.txt`;
  - `adjudication.py`: the labels;
  - `build_report.py`, `report_head.md` and `report_tail.md`;
  - this report.

## G. Limitations

- **Requests:** run 3's requests are rebuilt deterministically, not read from the record, because the harness does not save request text. The match rests on the same code at a clean `bac1a11`, the same read-only DB and the same local embedder, plus the one-to-one order and size correspondence with the 76 logged calls. The run 1 and run 2 requests (span only) are inferred from those commits' code and their input-token sizes, not rebuilt byte for byte.
- **Samples:** each case has one sample per run, so a 3-of-6 leak rate on 3 Claims is indicative, not a measured rate.
- **Reviewer:** the labels are one auditor's provisional findings. `2.9` (moderate confidence) and the borderline items in C.4 are where a second reviewer is most likely to differ.
- **Scope:** the 10 positives and 40 negatives were not re-audited. They were outside this assignment.
- **Historical framing:** whether an earlier Report actually said what “The earlier report found that…” attributes to it cannot be checked from Passages, and was not audited.
