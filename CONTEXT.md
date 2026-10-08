# InsightForge

An autonomous research platform: a user submits a research question and receives a cited intelligence report. Watched topics are re-researched on a schedule and changes are surfaced.

## Language

**Brief**:
The free-text research question a user submits. Also the standing question held by a Watchlist Item.
_Avoid_: query, prompt, topic (for the one-shot case)

**Run**:
One execution of the research pipeline for a Brief. Owns its Sub-tasks, Sources and Claims.
_Avoid_: job, session

**Report**:
The structured, cited output of a Run that reaches `COMPLETE`. Persisted across Runs.
_Avoid_: briefing, output

**Watchlist Item**:
A standing Brief plus a cadence. Spawns a new Run on each tick. Always uses web search, and never uploaded documents. It is `active`, or `blocked` with a reason when no enabled source is available; a blocked tick creates no Run, and the item returns to `active` on its own.
_Avoid_: monitor, subscription

**Sub-task**:
One scoped unit of research that a Brief is decomposed into. Belongs to a Run.
_Avoid_: task (reserved for the DeepAgents tool), branch

**Aspect**:
One distinct thing a Brief asks about. Every Sub-task covers exactly one Aspect, and every Aspect is covered by at least one Sub-task. A Brief with more than 6 Aspects is too broad to plan.
_Avoid_: theme, facet, topic

**Sub-task plan**:
The flat, dependency-free list of Sub-tasks produced for a Run (at least 3, at most 6). Each Sub-task has exactly one source type (`web`, `documents` or `memory`), a time horizon and a priority. Only source types that are both enabled and available may appear in it.
_Avoid_: task graph, DAG

**Source**:
Raw fetched material (a web page or an uploaded PDF), stored by reference.
_Avoid_: document, result

**Passage**:
One addressable paragraph of an Observation: a paragraph of a web page, or a chunk of an uploaded PDF (with its page and section heading). Its identity is its Observation plus its position, fixed when the Observation is first split, and its text is stored with that identity. An Observation is never re-split.
_Avoid_: chunk (outside PDF ingestion), snippet, excerpt

**Evidence item**:
A de-duplicated fact derived from one or more Passages, carrying a confidence level. Only Passages verified as supporting it count; it names one of them as its primary Passage, which is the one its citation shows. An item no Passage supports is dropped.
_Avoid_: fact, finding

**Claim**:
A statement in a Report that cites an Evidence item and is subject to fact-checking.
_Avoid_: fact, assertion

**Finding**:
A Claim normalised into a triple (subject, predicate, object) with a polarity, a scope and an As-of period, so it can be compared with the Baseline. A single-valued predicate (a price, a CEO) identifies a Finding by subject, predicate and scope; a multi-valued one (competes with, offers) by the whole triple and scope. The As-of period is never part of its identity, so a value can change across time. An older value never replaces a newer established one. Historical Claims never become Findings.
_Avoid_: change, update

**Observation**:
One fetch of a Source at a point in time, split into Passages. The Fact-Checker's re-fetch creates a new Observation with its own Passages; it never replaces the original.
_Avoid_: snapshot, version

**Extraction**:
The Run stage, after research and before synthesis, that identifies entities in a Run's Sources, one Source at a time, and flags conflicting facts by comparing the structured facts it extracted, never raw text from several Sources at once.
_Avoid_: entity pass

**Evidence bundle**:
The ranked, budget-limited set of Evidence items handed from synthesis to the Writer.
_Avoid_: context, corpus

**As-of period**:
The date or range a fact refers to, recorded with its scope on every Evidence item and Finding. It is distinct from the publication date. A period the Passage states is used as is. If a Source has neither a stated period nor a publication date and the Passage asserts a present state (not history, a prediction or an announcement), the period is derived from the Observation's date and marked as derived; that date says when the state was observed, never when it began. Otherwise the period is unknown, is never assumed to match another period, and is flagged as temporally ambiguous rather than counted as a change or contradiction.
_Avoid_: date (alone), timestamp, effective date (for a derived period)

**Confidence**:
`HIGH | MEDIUM | LOW`, a property of an Evidence item. A Report section's label is the lowest Confidence among its items. An item with a genuine contradiction (same entity, predicate and scope, with overlapping As-of periods, from any Source, independent or not) is conflicting and `LOW`. Different values for non-overlapping periods or different scopes are not contradictions.
_Avoid_: certainty, score (reserved for credibility)

**Independent Sources**:
Two Sources are independent only if they have different domains and neither cites the other. Independence is counted over distinct Sources, never over Passages.
_Avoid_: separate sources

**Run summary**:
A compressed record written once per completed Run (Brief, Sub-task themes, key entities, Gaps & limitations). The Planner reads only the last 3 for the same Watchlist Item, or for the same user on one-shot Runs.
_Avoid_: session, session summary, transcript

**Vague Brief**:
A Brief from which the Planner cannot produce at least 3 Sub-tasks with a concrete scope and time horizon.
_Avoid_: unclear brief

**Material change**:
A Finding that is new, whose object differs from its established value beyond tolerance, or that current evidence affirmatively shows is no longer true. A Finding that is merely absent from a Run is Not reconfirmed, never a Material change. Rephrasing is never a Material change.
_Avoid_: update, diff

**Not reconfirmed**:
A Finding in the Baseline that the latest Run neither confirmed nor contradicted. It keeps its last established value and raises no Alert. After several consecutive such Runs it is shown as unconfirmed, still without an Alert.
_Avoid_: removed, missing, stale

**Historical Claim**:
A Claim backed only by prior Reports rather than by Sources fetched in the current Run. It is shown with its original date as background, never as a current fact, and it never establishes or confirms a Finding.
_Avoid_: memory claim, old claim

**Claim verdicts**:
A sampled Claim is `verified`, `unverified` (re-fetched, no match at or above the similarity threshold) or `unchecked` (re-fetch failed). Only `unverified` Claims are marked `[UNVERIFIED]`; `unchecked` ones are excluded from the pass rate.

**Failed Run**:
A Run in the terminal `FAILED` state: planning could not produce at least 3 Sub-tasks, every Sub-task failed, the Writer exhausted its retries, or a Run-level limit tripped. It produces no Report, fires no Alert, and never feeds the memory researcher. Its raw material expires after a retention period; a small failure record survives. A single failed Sub-task does not fail the Run; it is listed in Gaps & limitations.
_Avoid_: errored run, aborted run

**Baseline**:
A Watchlist Item's latest established state of each Finding, with the provenance and date that established it. It is not a Report. The first successful Run creates it and raises no Alert. Each successful Diff updates the Findings that Run established, changed or showed to be no longer true, and leaves Not reconfirmed ones as they were. A failed Diff leaves it unchanged, so the next Diff shows the net change.
_Avoid_: previous report

**Alert**:
One per Run that has at least one Material change. Holds the change summary (added, changed, no longer true), links to the new Report and, for each change, to the Report that established the earlier value, and notes any intervening Runs whose Diff failed. A change resting on a derived As-of period is reported as detected on that date, never as having happened on it. Has a read/unread state. Optionally also delivered to a webhook.
_Avoid_: notification, event

**Share link**:
A signed, read-only, expiring, revocable link to one Report. Exposes the Report, its fact-check summary and web Source metadata; hides excerpts from uploaded PDFs, the Run log and all other Reports.
_Avoid_: public link

**User**:
One of a small, predefined set of people (3 to 4 personas) declared in a configuration file. Owns Briefs, Runs, Reports, Watchlist Items and Alerts.
_Avoid_: account, customer, analyst (as a system term)

**Credibility score**:
A number from 0 to 1 on every Source. Web Sources are scored from domain type, recency and byline; uploaded documents from document type and date; memory Sources inherit the scores of the Sources behind them.
_Avoid_: trust score, rank

## Relationships

- A **Brief** starts one **Run**; a **Run** that reaches `COMPLETE` yields exactly one **Report**, and a **Failed Run** yields none.
- A **Watchlist Item** holds a **Brief** and spawns a new **Run** per tick.
- A **Run** owns its **Sub-tasks**, **Sources** and **Claims**. The **Report**, its **Claims**, every **Source**, **Observation** and **Passage** it cites, and extracted entities outlive it. Material nothing retained refers to is eventually removed.
- **Source** → **Observation** → **Passage** → **Evidence item** → **Claim** → **Finding**: each is a distinct stage of the same information.
- A **Source** has a content-derived identity, is immutable within a **Run**, and gains **Observations** over time.
- Only **Extraction**, synthesis and the Fact-Checker read **Passage** text, one bounded batch per model call. Researchers see only their own Sources; the Planner and Writer never see Passage text.
- A **Watchlist Item**'s **Brief** must not be **vague**; this is checked at creation, so scheduled **Runs** never ask clarifying questions.
- A **Watchlist Item** has at most one active **Run**; an overlapping tick is skipped, and missed ticks are not made up.
- A **Report**'s **Sources**, **Observations** and **Passages** are kept for as long as the **Report** exists.
- Every **Claim** must cite an existing **Evidence item**; a Writer that cannot meet this after 2 retries produces a **Failed Run**.
- A **Run** against a **Baseline** raises an **Alert** only when it finds a **Material change**.
- Prior **Reports** may give a **Run** background, but only **Sources** fetched in that **Run** can establish or confirm a **Finding**.
- **Reports** are never deleted.
- Uploaded documents are ingested at the start of a **Run**, are visible only to that **Run**, and must be uploaded again for a later one. Their chunks are still kept while a **Report** cites them. A **Watchlist Item** therefore cannot use uploaded documents as a source.
- Everything a **User** creates is owned by that **User**.
- The Fact-Checker verifies **Claims** against **Sources**; the Diff Agent compares **Findings**.
