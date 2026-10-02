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
The structured, cited output of exactly one Run. Persisted across Runs.
_Avoid_: briefing, output

**Watchlist Item**:
A standing Brief plus a cadence. Spawns a new Run on each tick.
_Avoid_: monitor, subscription

**Sub-task**:
One scoped unit of research that a Brief is decomposed into. Belongs to a Run.
_Avoid_: task (reserved for the DeepAgents tool), branch

**Sub-task plan**:
The flat, dependency-free list of Sub-tasks produced for a Run (at most 6). Each Sub-task has exactly one source type (`web`, `documents` or `memory`), a time horizon and a priority.
_Avoid_: task graph, DAG

**Source**:
Raw fetched material (a web page or a PDF chunk), stored by reference.
_Avoid_: document, result

**Evidence item**:
A de-duplicated fact derived from one or more Sources, carrying a confidence level.
_Avoid_: fact, finding

**Claim**:
A statement in a Report that cites an Evidence item and is subject to fact-checking.
_Avoid_: fact, assertion

**Finding**:
A Claim viewed across two Reports, normalised so it can be compared by the Diff Agent.
_Avoid_: change, update

**Observation**:
One fetch of a Source at a point in time. The Fact-Checker's re-fetch creates a new Observation; it never replaces the original.
_Avoid_: snapshot, version

**Extraction**:
The Run stage, after research and before synthesis, that identifies entities across all of a Run's Sources and flags conflicting facts.
_Avoid_: entity pass

**Evidence bundle**:
The ranked, budget-limited set of Evidence items handed from synthesis to the Writer.
_Avoid_: context, corpus

**Confidence**:
`HIGH | MEDIUM | LOW`, a property of an Evidence item. A Report section's label is the lowest Confidence among its items.
_Avoid_: certainty, score (reserved for credibility)

**Independent Sources**:
Two Sources are independent only if they have different domains and neither cites the other.
_Avoid_: separate sources

**Run summary**:
A compressed record written once per completed Run (Brief, Sub-task themes, key entities, Gaps & limitations). The Planner reads only the last 3 for the same Watchlist Item, or for the same user on one-shot Runs.
_Avoid_: session, session summary, transcript

**Vague Brief**:
A Brief from which the Planner cannot produce at least 3 Sub-tasks with a concrete scope and time horizon.
_Avoid_: unclear brief

**Material change**:
A Finding whose triple (subject, predicate, object) is new, missing, or whose object differs beyond tolerance. Rephrasing is never a Material change.
_Avoid_: update, diff

**Claim verdicts**:
A sampled Claim is `verified`, `unverified` (re-fetched, no match at or above the similarity threshold) or `unchecked` (re-fetch failed). Only `unverified` Claims are marked `[UNVERIFIED]`; `unchecked` ones are excluded from the pass rate.

**Failed Run**:
A Run in the terminal `FAILED` state: every Sub-task failed, or the Writer exhausted its retries. It produces no Report and fires no alert. A single failed Sub-task does not fail the Run; it is listed in Gaps & limitations.
_Avoid_: errored run, aborted run

**Baseline**:
The most recent completed Report for a Watchlist Item, against which a new Run is diffed. The first Run stores a Baseline and raises no alert.
_Avoid_: previous report

**Alert**:
One per Run that has at least one Material change. Holds the change summary (added, removed, changed) and links to both Reports; has a read/unread state. Optionally also delivered to a webhook.
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

- A **Brief** starts one **Run**; a **Run** yields exactly one **Report**.
- A **Watchlist Item** holds a **Brief** and spawns a new **Run** per tick.
- A **Run** owns its **Sub-tasks**, **Sources** and **Claims**; only the **Report** and extracted entities outlive it.
- **Source** → **Evidence item** → **Claim** → **Finding**: each is a distinct stage of the same information.
- A **Source** has a content-derived identity, is immutable within a **Run**, and gains **Observations** over time.
- **Extraction** reads all of a **Run**'s **Sources**; researchers never see each other's output.
- A **Watchlist Item**'s **Brief** must not be **vague**; this is checked at creation, so scheduled **Runs** never ask clarifying questions.
- A **Watchlist Item** has at most one active **Run**; an overlapping tick is skipped, and missed ticks are not made up.
- A **Report**'s **Sources** and **Observations** are kept for as long as the **Report** exists.
- Every **Claim** must cite an existing **Evidence item**; a Writer that cannot meet this after 2 retries produces a **Failed Run**.
- A **Run** against a **Baseline** raises an **Alert** only when it finds a **Material change**.
- Uploaded documents are ingested at the start of a **Run**, are scoped to that **Run**, and must be uploaded again for a later one.
- Everything a **User** creates is owned by that **User**.
- The Fact-Checker verifies **Claims** against **Sources**; the Diff Agent compares **Findings**.
