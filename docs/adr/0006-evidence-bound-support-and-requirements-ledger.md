# Evidence-bound support, one Evidence pipeline and a requirements ledger

Status: accepted for implementation on `fix/t18-research-integrity` (draft PR into `t18-verification`). Offline-verified only; live model behaviour is unproven until a separately authorised, budget-capped validation runs. Refs #19, #75, #76. Builds on ADR-0005 (model-first semantics, invariants in code); it does not replace it.

## Context

T18 Stage C (`6cdcf32`) and the B-1 audits exposed failures that are structural, not lexical:

1. **A lexical gate decided semantics.** `literal_problem` refused any candidate whose capitalised words the Passage did not repeat, before the judge was asked. The uploaded Northwind report's "6 programmes in clinical trials" was lost this way because the model labelled the subject "Northwind Health pipeline". Every fix (opener lists, document credit, credited-name removal) widened the rule set without making it right.
2. **The judge could accept qualifiers the cited Passage never states** (#75: `1.5` market definition, `2.9` capex, `3.3` solid-state). Nothing made the judge show where each part of a statement is stated.
3. **Two model passes invented facts independently.** Extraction and drafting both read the same Passages and produced different statements; only drafting fed the Report.
4. **Lost work looked like no work.** A drafting or extraction reply cut at the output limit, or invalid JSON, was logged and returned as an empty batch.
5. **Completeness was tracked per Sub-task, not per question.** A Sub-task with any kept item counted as covered, so "Gaps & limitations" could say nothing while a requested fact was missing.
6. **Cosine similarity acted as a verdict.** The Fact-Checker never asked the judge about a cited Passage that scored under 0.85.

## Decision

### 1. One support contract (`domain/support.py`, `pipeline/support.py`)

Synthesis and the Fact-Checker judge support through one function, `judge_units`. Input: one proposition, the cited units exactly as shown (a Passage, a window of one, or a span in the context of its own Passage) and, per unit, optional identity context. Output per unit: `SUPPORTED | CONTRADICTED | INSUFFICIENT_EVIDENCE | VERIFICATION_ERROR`, with problems, excerpt offsets and reasons, logged as `support_judged` (no source text).

For SUPPORTED the model must return a **support map**: the statement split into parts, each with exact words of the unit that state it. Code then checks, and fails closed:

| Check | Failure verdict |
|---|---|
| Every excerpt occurs in the shown unit (whole words; case, punctuation, quotes, spacing ignored) | `VERIFICATION_ERROR` (fabricated quote) |
| The parts cover every content word of the statement | `INSUFFICIENT_EVIDENCE` (an unmapped qualifier) |
| Every figure of the statement is stated by the excerpts (exact quantities: "$71 million" = "71 million dollars") | `INSUFFICIENT_EVIDENCE` |
| An identity part is used only where identity context was given, carries no figure, and is the document's own entity or a run of its words | `INSUFFICIENT_EVIDENCE` |
| Missing, conflicting or invalid reply for a unit | `VERIFICATION_ERROR` |

Whether an excerpt *means* its part (paraphrase, "expects" versus "received") stays the model's judgment. Code never turns a rejection into support. Every Passage-reading call (Extraction and support) is sized against `PASSAGE_TOKEN_CAP` on its user message, excluding the fixed system prompt.

**Trade-off accepted.** The retired lexical gate also refused one class deterministically that the new contract leaves to the judge: a competitor's sentence inside the company's own report attributed to the company (gold case NW-N07). Code refuses an identity part naming anyone but the document's entity, but cannot tell that a quoted Passage gives the fact to someone else. B-1 recorded 0 of 40 wrong-company or wrong-measure negatives judged SUPPORTED; the live gate repeats NW-N07 to measure it.

### 2. Identity-only context (trust boundary)

Only an uploaded document whose own text establishes its company (`document_entity`: a capitalised run in the file name and in at least two chunks) gives identity context, and only to its own Passages, for current and Historical Claims alike (identity is a property of the cited document; nothing is removed from the Claim): "Passage [n] is from an uploaded document of Northwind Health." It may establish who an unnamed subject is. It never supplies a figure, measure, market, unit, period, product, place or qualifier, and never applies to another company (code refuses an identity part that is not the entity). Web text never gets identity context. No unit is ever shown with its Source title, a heading outside it, a neighbouring Passage or another user's material. A qualifier from another Passage counts only if that Passage is itself cited and judged.

### 3. One Evidence pipeline

Extraction is the only pass that proposes facts. It reads one Source at a time (ADR-0002) in bounded batches, told which Sub-tasks found the Source and which research requirements they serve, and anchors each fact to exact Passages. Synthesis no longer drafts: it turns extracted facts into candidates, merges the ones that say the same thing (`merge_identity`, unchanged), verifies each through the support contract, labels Confidence and ranks. The 3-facts-per-Passage cap is gone; output size is handled by §4. `DRAFT_STRUCTURED_OUTPUT` and `DRAFT_PASSAGE_CAP` now apply to Extraction. Two Sources whose supporting Passages are the same text (word overlap of at least 80 %) are not independent corroboration (syndicated or re-posted copies). The retired lexical gate `literal_problem` is kept only as the frozen baseline that the ADR-0005 benchmark and the historical T18 replays measure.

### 4. Work units with bounded recovery

Every model batch that reads Passages (extraction, support) is a work unit with a logged status: `parsed`, `truncated` (stop reason at the output limit, or JSON that ends early), `invalid`, `split` (retried as two halves), `exhausted`. A truncated or invalid extraction batch of more than one Passage is split and retried, at most two levels deep and at most `work_unit_retry_cap` retries per Run; a successful half is never redone. Exhausted units are listed in the Report's gaps with the Passages they covered, and the requirements those Passages serve cannot be marked supported by them. A resumed Run does not re-extract a Source whose same Observations it already extracted. Support calls that fail are VERIFICATION_ERROR per unit (not split): their cost is one item, recorded.

### 5. Requirements ledger

After the plan is accepted, one light-model call turns the Brief into 1-12 atomic requirements (`Rk`: question, required or optional, linked Aspects). If the call fails, each Aspect becomes one requirement (`origin: "aspects"`, logged). Status per requirement: `supported`, `contradictory`, `no_evidence`, `incomplete` (a failed Sub-task or exhausted work unit touched it), then `reported` once a Claim cites one of its items. Extraction links facts to requirement ids; code accepts only known ids. The ledger lives in graph state and the Report body (`ledger`); the Writer sees only a compact coverage index, outside the 6,000-token Evidence budget. Budget compression protects the last item supporting a required requirement before anything else and logs any cut. Every requirement that is not supported becomes a fixed-wording gap line, so "Gaps & limitations" always matches the ledger.

### 6. Analytical Claims with several premises

`Claim.premises` (default empty) lists further Evidence ids a comparison or conclusion rests on; `evidence_id` stays the primary citation, so every existing reader keeps working. Numbers and names are checked against the union of the cited items. Before publication, every Claim with premises is judged jointly (`judge_joint`) against the primary Passages of all its items; one that is not SUPPORTED is removed and logged. The citation drawer and the Markdown/PDF export show every premise's Source. Claims with premises never become Findings and never become Historical background for a later Run (a Historical item carries one lineage). No new number may be derived: arithmetic is not performed by the model.

### 7. Fact-Checker

Unchanged in sample size (20 %, minimum 5) and verdict vocabulary, and still one bounded call per sampled Claim (weakest candidates dropped to fit the cap). The cited Passage's best span is always judged: the stored chunk for a PDF, the re-fetched counterpart for a web page; cosine similarity only adds and ranks up to 3 further candidates, none evicted. When the page does not hold the cited Passage (changed, ambiguous, stored under another Source, or not looked up), the Passage as cited is judged in the same call and recorded as `ClaimVerdict.cited_support`; it can never verify the Claim, and a Claim it supported is reported as page drift. The support contract replaces the old entailment call and the document-credit exception. A sampled Claim with premises is judged jointly against all its premises. This departs from the assignment's literal "cosine ≥ 0.85 means verified"; the 0.85 threshold remains the candidate-retrieval floor.

## Compatibility

New fields have defaults (`Claim.premises`, `EvidenceItem.requirement_ids`, `Citation.premises`, Report body `ledger`), so stored Reports, Historical Claims, Watchlist Baselines and Diffs parse unchanged. Old Reports have no ledger and show no requirement status. Prompt versions: `support-1` replaces the F2 entailment prompt for new Runs; `writer-3` replaces `writer-2`; extraction is `extract-2`. T18 records, archived hashes, judge scores and thresholds are not changed.

## Exit criteria (to call the redesign verified, not just implemented)

1. Offline: gold positives retained, gold negatives refused wherever code can refuse them, Stage C fixture 13/13 with scripted honest models, full suite green under the network guard.
2. Live (separately authorised, hard-capped): the 27 historical Claims against the versioned gold (24 SUPPORTED / 3 INSUFFICIENT), the 40 B-1 negatives and 10 positives, and the qualifier-leak negatives, with repeated runs on ambiguous negatives; then a new Stage-C-equivalent two-Brief run. Until then the state is "implemented, live validation pending".
