# InsightForge technical design

Status: confirmed. Vocabulary is defined in `../CONTEXT.md`; decisions that are hard to reverse are in `adr/`. Source spec: `../assignment_04_market_intelligence.md`.

## 1. Scope and users

- Build the research pipeline first, then scheduling and diffing, then the API and UI on top of them.
- Users are 3 to 4 personas declared in `users.yml` (see section 9). Users are strictly isolated from each other.
- Single process, single node. Postgres, a worker queue and real accounts are deliberate non-goals.
- Reports are never deleted. There is no delete endpoint.

## 2. Pipeline

LangGraph is the outer graph. The Planner is the `create_deep_agent()` root agent, and it spans the `PLANNING` and `RESEARCHING` nodes on one checkpointed thread (ADR-0001).

```
INGESTING -> PLANNING -> RESEARCHING -> EXTRACTING -> SYNTHESIZING -> WRITING -> FACT_CHECKING -> COMPLETE
                |  ^                                                    ^  |
                v  |                                                    +--+ (retry, max 2)
  AWAITING_CLARIFICATION -> RESUMING                     any stage -> FAILED
```

- `INGESTING` is skipped when there are no uploaded documents (section 5).
- **Submission check.** The Planner may assign only source types that are enabled and available. Web is always available. Uploaded documents are available when at least one was uploaded for this Run. Memory is available when the User has at least one eligible Report (section 5). A Brief with no available source type is rejected with 422 before a Run is created. This covers "all toggles off".
- **Planning.** The Planner produces the Aspects of the Brief and a `SubTaskPlan` in which every Sub-task covers one Aspect.
  - **Bounds.** The plan must hold 3 to 6 Sub-tasks, each covering exactly one Aspect, with every Aspect covered. A plan outside the bounds, or with more than 6 Aspects, gets exactly one corrective re-prompt that states the violation. For too many Aspects, the re-prompt asks the Planner to consolidate genuinely related Aspects, never to drop one.
  - **Too many.** If there are still more than 6 Sub-tasks, they are trimmed deterministically by priority while keeping at least one per Aspect. If there are still more than 6 Aspects, trimming is impossible and the Run fails with that reason.
  - **Too few.** If there are still fewer than 3, the Run fails, including after a clarification timeout.
  - **Logging and limits.** Corrections, trimmed Sub-tasks, assumptions and failure reasons go to the Run log. Corrective attempts count against the Run-wide limits.
- **Clarification.** `AWAITING_CLARIFICATION` is a non-terminal state entered when the Planner asks its one question. It uses LangGraph interrupt and resume with the checkpoint in the database.
  - **Timeout job.** Entering the state schedules a one-shot APScheduler job at `entered_at + timeout` (default 30 minutes), persisted in the job store.
  - **Resolution.** `POST /research/{run_id}/clarify` and the timeout job both resolve the question through one compare-and-set on the Run's state. The first one wins, a late answer gets 409, and an answer cancels the job.
  - **Resuming.** The resolution (the answer, or the timeout) is stored on the Run before it moves to `RESUMING`, an internal state the UI does not show as a stage. The graph then resumes into `PLANNING`. On a timeout the Planner proceeds on a best-effort reading and records the assumption in Gaps & limitations.
- **Researching.** The graph resumes the same Planner thread, which dispatches one `task` per Sub-task. Each Sub-task is a todo in `write_todos`. After the node returns, the graph verifies the result (ADR-0003):
  - **Repair.** Sub-task IDs with no result get exactly one repair round. The graph resumes the Planner with only the missing IDs and their original approved scopes. The Planner must not create Sub-tasks, change scopes or redispatch successful ones.
  - **What is retried.** Sub-tasks that explicitly failed are not retried. Only missing ones are.
  - **Limits.** The repair round uses the remaining per-Sub-task and Run-wide budgets. A Sub-task whose budget has expired is marked `Failed` without redispatch.
  - **Idempotency.** Each Sub-task ID has one authoritative result. A late or duplicate result never overwrites a successful one.
  - **Outcome.** If at least one Sub-task succeeds, the Run proceeds to `EXTRACTING` and the failures go in Gaps & limitations. If every Sub-task fails, the Run fails.
- `EXTRACTING` is its own stage so Researchers stay isolated (ADR-0002).
- `FAILED` is terminal and produces no Report. It happens when planning cannot produce a valid plan, every Sub-task fails, the Writer exhausts its retries, or a Run-level limit trips.

## 3. Stage contracts

One pydantic model per transition, defined in `domain/`. A stage reads its input model plus the stores, and nothing else.

| Transition | Model | Contents |
|---|---|---|
| submit | `Brief` | text, source toggles, document IDs |
| plan | `SubTaskPlan` | Aspects, and a flat list of Sub-tasks: `id, aspect_id, query, source_type, time_horizon_months, priority` (1 is most important) |
| research | `ResearchResults` | per Sub-task: `source_refs[]` (`source_id`, title, one-line summary), status |
| extract | `ExtractionResult` | entities, structured facts with Passage references, conflicts |
| synthesize | `EvidenceBundle` | sections of Evidence items, each with Confidence, `as_of_period`, scope, supporting and contradicting Passage IDs, and one primary Passage ID |
| write | `ReportDraft` | sections of Claims, each with an `evidence_id` and whether it is a Historical Claim |
| fact-check | `VerifiedReport` | the draft plus Claim verdicts and the fact-check summary |

Graph state carries IDs and small structured values only, never Source bodies or Passage text. A test asserts that serialised state holds no body above a size limit.

## 4. Context engineering

- **Scratch store.** Sources, Observations and Passages live in our database behind a `SourceStore` interface. `read_source(source_id)` is a thin tool over it. PDF chunk text lives in Qdrant, and the same interface resolves those IDs. Source IDs are content-derived and immutable within a Run.
- **Passages.** An Observation is split into Passages once, when it is first stored. A web body is split into paragraphs, and an uploaded PDF's Passages are its Docling chunks, chunked by paragraph where the structure allows and keeping page and section heading.
  - **Identity.** A Passage is identified by `(observation_id, passage_index)` and its text is stored with that identity.
  - **No re-splitting.** An existing Observation is never re-split, so a later change to parsing cannot break a citation.
- **Raw-text access.** Passage text is retrieved through `SourceStore` only, one bounded batch per model call:

  | Stage | Raw-text access |
  |---|---|
  | Researchers | Only their own fetched Sources, and only inside their acquisition tools |
  | Planner | None. It plans and delegates using references |
  | Extraction | Passage batches from one Source |
  | Synthesis | Passage batches from one Sub-task |
  | Writer | None. It reads only the Evidence bundle |
  | Fact-Checker | Original and newly fetched Passages, only to verify sampled Claims |

- **Per-call cap.** Every model call that reads Passage text is limited by a configurable token cap. A batch over the cap is split, never silently truncated. This cap and the Evidence bundle budget are separate limits, and each has its own test. A test captures model-call payloads and asserts that they respect the cap.
- **Isolation.** Researcher subagents are defined with their own `tools=` and receive only their Sub-task. Never use fork mode. A researcher model never sees a Source body: the fetch tool writes the one-line summary in its own bounded call, and the researcher sees only `{source_id, title, summary}`.
- **Budget.** The Evidence bundle respects a token budget (default 6,000). Overflow is compressed, never truncated, and each cut is logged with its reason and the item's Confidence.
- **Memory.** At start the Planner reads the last 3 Run summaries for the same Watchlist Item, or for the same User on one-shot Runs. It never reads transcripts.
- **Compaction.** DeepAgents compacts history automatically. There is no `summarization=True` flag, and the Run log records each compaction so the README's context-blowout evidence can be produced.
- **Planner todos.** `write_todos` tracks Sub-task completion. Whether `TodoListMiddleware` has to be passed explicitly is checked at build time (section 12).

## 5. Agents

- **Planner.** The `create_deep_agent()` root agent. Takes the Brief and the available source types and produces the Aspects and the `SubTaskPlan`, then dispatches the Researchers via `task` (section 2). A Vague Brief gets exactly one clarifying question. A Watchlist Item's Brief is validated for vagueness when it is created.
- **Ingestion.** Docling extracts and chunks the PDFs (at most 10, 20 MB each) into the `doc_chunks` collection, with `{run_id, owner_id, source_filename, page, section_heading}`. Each chunk is a Passage.
- **Web Search.** The provider is chosen by setting. Fetches the full body of the top 5 results, splits it into Passages, scores credibility, skips paywalled pages and logs them. The one-line summary is written inside the fetch tool.
- **Doc Reader.** Semantic search over `doc_chunks`, k = 8, returning chunk IDs and never text.
- **Memory researcher.** Searches `report_sections` for the User's prior completed Reports. It never searches Failed Runs, and in a Watchlist Run it never searches Reports from the same Watchlist Item. What it finds is background only: it produces Historical Claims, never current Evidence.
- **Extractor.** Works one Source at a time in Passage batches. It persists entities and structured facts with their Passage references to the `entities` collection as it goes. Conflicts are then detected over the structured facts grouped by entity, never over raw text, and marked `conflict: true`.
- **Synthesis.** Works one Sub-task at a time over that Sub-task's Passages and drafts Evidence items, each with its supporting Passage IDs, `as_of_period` and scope. After all Sub-tasks are done, it deduplicates and merges over the structured items, then ranks and compresses to budget.
  - **Validation.** Every candidate Passage first passes a deterministic provenance check and a literal-value check (every number and named entity in the item appears in the Passage). Then one bounded entailment call per Evidence item judges its candidates as `SUPPORTED`, `CONTRADICTED` or `INSUFFICIENT_EVIDENCE`. The call is chunked if the candidates exceed the per-call cap and counts against the Run's token cap.
  - **Confidence and primary Passage.** Only `SUPPORTED` Passages count toward Confidence. The primary Passage is the supported one with the highest Source credibility, then the highest relevance to the item, then the lowest Passage ID. An item with no supported Passage is dropped and logged.
  - **Contradictions.** A `CONTRADICTED` Passage triggers a context check. If it concerns the same entity, predicate and scope with an overlapping As-of period, the item is marked conflicting and its Confidence is `LOW`, whatever the Sources' independence. The item links the conflicting Extraction facts and keeps both the supporting and contradicting Passage references. Non-overlapping periods or different scopes are distinct facts.
  - **As-of period.** A period the Passage states is used as is. If a Source has neither a stated period nor a publication date and the Passage asserts a present state, `as_of_period` is the Observation's date with `derived_from = observation_date`. This applies only to present-state assertions, not to history, predictions or announcements. Otherwise the period is `UNKNOWN` and flagged as temporally ambiguous. Every derived period is logged. The present-state judgement is part of the entailment call.
  - **Ranking.** Evidence items are sorted by the tuple `(recency_bucket desc, credibility_bucket desc, relevance_to_brief desc, subtask_priority asc, evidence_id asc)`. Relevance is cosine similarity between the item and the Brief. Bucket thresholds are settings. Ranking never changes provenance, independence or Confidence.
- **Writer.** Produces the Report with the required sections. Every Claim cites an existing Evidence item, and its inline citation shows the item's primary Passage. Every number and named entity must appear in the item. Historical Claims show their original date inline. Violations trigger up to 2 retries with the offending Claims listed as a constraint.
- **Fact-Checker.** Samples 20% of Claims (minimum 5), seeded from the Run ID. A Report with fewer than 5 Claims has all of them checked. Historical Claims are eligible. Verdicts are `verified | unverified | unchecked` (section 7).
- **Diff Agent.** Compares Findings against the Baseline (section 8).

## 6. Persistence

- **Database (SQLite behind repository interfaces).** Users' Runs, Sub-tasks, Sources, Observations, Passages, Reports, Watchlist Items, Baselines, the Diff log, Alerts, `run_events`, failure records, revoked share links, and the APScheduler job store.
- **Qdrant.** `doc_chunks`, `entities` (cross-run), `report_sections` (prior Reports). One configurable embedding model, with its name recorded in each collection's metadata. Every query is filtered by `owner_id` inside the repository. `doc_chunks` is Run-scoped for visibility only: a Run searches only its own uploads, but chunks are kept while a Report cites them.
- **Progress.** The pipeline appends to `run_events`. The SSE endpoint tails it with a cursor (`Last-Event-ID`), so a reconnecting client loses nothing.
- **Retention.** A periodic sweep removes material based on what still refers to it, not on the originating Run's status.
  - **Protected material.** A Source, Observation, Passage or document chunk cited by a Report, or by the evidence behind a Baseline Finding, is never removed.
  - **Failed Runs.** Their raw material expires after a configurable retention period. A small failure record survives it: the reason, timestamps, the stage, and error codes. Failed Run material never reaches the memory researcher.
  - **Completed Runs.** Material from a completed Run that nothing retained refers to becomes eligible for removal after the same period.
  - **Entities.** An extracted entity whose underlying Passage is removed keeps the entity with its provenance marked unavailable, never a dangling reference.

## 7. Credibility, Confidence and fact-checking

- **Credibility score** (0 to 1) exists on every Source: web by domain type, recency decay and byline; uploaded documents by document type and date; memory Sources inherit from the Sources behind them.
- **Confidence** belongs to an Evidence item and counts only its `SUPPORTED` Passages:
  - `HIGH` needs 2 or more Independent Sources, counted over distinct Sources, never Passages.
  - `MEDIUM` is a single high-credibility Source.
  - `LOW` is a single low-credibility Source or a conflicting item (section 5).

  A section's label is the lowest among its items.
- **Fact-check.**
  - **Re-fetch.** Re-fetching a web Source creates a new Observation with its own Passages. The original Observation, its Passages and the Claim's citation are unchanged.
  - **Verdict.** The verdict takes the maximum similarity between the Claim and any new Passage. A Claim passes at similarity of at least 0.85 (configurable), and a numeric Claim also needs its number in the matched Passage. The verdict references the matching Passage.
  - **Pass rate.** `unchecked` (the re-fetch failed) is excluded from the pass rate.
- **Non-web Sources.** For uploaded documents, run the same max-over-Passages check against the stored Passages of the cited document, which are the original text and not the Evidence item. For `memory` Sources and Historical Claims, follow the inheritance chain to the original Sources and check those, re-fetching if they were web pages. The verdict records which kind of check ran.

## 8. Scheduling and diffing

- **Ticks.** APScheduler runs one Run per Watchlist Item per tick, using the same pipeline. At most one active Run per item. Overlapping and missed ticks are skipped.
- **Watchlist sources.** A Watchlist Item always has web search enabled and can never use uploaded documents. It may add Prior Reports for background. Both rules are checked at creation.
- **No Watchlist status.** Because web search is always enabled and always available, a tick can never lack a source, so a Watchlist Item has no `blocked` state and there is no per-tick source check. A missing provider key stops the app at startup (section 10). A provider outage during a Run ends in a Failed Run, which raises no Alert and leaves the Baseline unchanged. The dashboard shows each item's recent Failed Runs from Run data.
- **Baseline.** The Baseline is the latest established state of each Finding for the item, with the provenance, Observation date and As-of period that established it. It is not a Report (ADR-0004).
  - **First Run.** The first successful Run creates the Baseline without a Diff and raises no Alert.
  - **Successful Diff.** Each successful Diff updates the Findings that the Run established, changed or showed to be no longer true. It leaves Not reconfirmed Findings unchanged.
  - **Failed Diff.** A permanently failed Diff leaves the Baseline unchanged. The next successful Diff records the intervening Runs whose Diffs failed in the Diff log. Its result is the net change since the Baseline, not every change that happened in between, and the UI says so.
  - **No manual re-diff.** There is no manual re-diff.
- **Findings.** A Finding is a normalised triple (subject, predicate, object) with a polarity, a scope and an As-of period. Only Claims backed by Sources fetched in this Run become Findings. Historical Claims never do.
  - **Predicate vocabulary.** A controlled vocabulary declares each predicate's cardinality.
  - **Identity.** A single-valued predicate identifies a Finding by `(subject, predicate, scope)`. A multi-valued one identifies it by `(subject, predicate, object, scope)`. The As-of period is never part of the identity, so values are compared across time.
  - **Other Findings.** An LLM judge handles only Findings outside the vocabulary or that do not reduce to a triple.
- **Material change.**
  - **What counts.** A Material change is a new Finding, a single-valued object that differs from its established value beyond tolerance, or a Finding shown to be no longer true. Only a fresh replacement value or an explicit negated Finding from this Run's Sources shows that.
  - **Absent Findings.** An absent Finding is Not reconfirmed, never removed. The Baseline counts consecutive Not reconfirmed Runs per Finding. After K Runs (default 3) the Finding is shown as unconfirmed on the dashboard and in the Diff view, without an Alert.
- **Temporal rules.** An older value never replaces a newer established one, and older observations are kept for audit. A value with an `UNKNOWN` As-of period is flagged as ambiguous and is neither a change nor a contradiction. A derived As-of period is shown with its provenance in the Diff view.
- **Where the Diff runs.** The Diff Agent runs in the scheduler, outside the pipeline graph, after a Run reaches `COMPLETE`. A Diff failure is logged, retried once, and never changes the Run's status. Manual one-shot Runs are not diffed.
- **Alerts.** One Alert per Run with at least one Material change.
  - **Links.** It links to the new Report and, for each change, to the Report that established the earlier value. It notes any intervening failed Diffs.
  - **Wording.** A change resting on a derived As-of period reads "Change detected on [date]", never "Changed on [date]".
  - **Webhook.** The Alert is optionally POSTed to the webhook with retry and backoff. Webhook failure never fails the Run.

## 9. API and identity

- `POST /research/run`, `POST /research/{run_id}/clarify`, `GET /research/status` (SSE), `/watchlist` CRUD, `/reports` (list, retrieve, export), plus Alerts.
- Identity: a persona picker in the UI sets a session cookie holding the persona ID, signed with the app secret and marked HttpOnly. A FastAPI dependency resolves it to a `User`. This is persona selection and not security, and the README says so. One User never sees another's data, and a request for it returns 404. Personas in `users.yml` carry `id`, `display_name` and optional defaults (source toggles, webhook URL), with no keys.
- Share links are signed JWTs holding the Report ID and expiry, revocable through a table. They expose the Report, the fact-check summary and web Source metadata, and hide uploaded-document excerpts, the Run log and every other Report.
- The citation drawer resolves a Claim's Evidence item to its primary Passage and shows the Source title, publication date, credibility score and the Passage text, for every Claim, independently of the Fact-Checker's sample.
- Export: Markdown, PDF, and the share link. PDF is rendered with Playwright's headless Chromium from the same HTML the viewer uses, behind an interface so another renderer can replace it. The README notes the one-time `playwright install chromium`.

## 10. Configuration and limits

One typed settings object (pydantic-settings, from the environment and `.env`). The app refuses to start when a required secret for the chosen provider is missing. `.env.example` is committed and `.env` is ignored.

- Model name, provider keys, search provider (`tavily | brave | serpapi`), Qdrant URL, database URL, personas file path.
- Evidence token budget 6,000, per-call Passage token cap, Sub-tasks 3 to 6, Fact-Checker rate 20% (minimum 5) and similarity threshold 0.85, recency and credibility bucket thresholds, clarification timeout 30 minutes, Not reconfirmed display threshold 3, retention period, share-link TTL 7 days, JWT secret.
- Limits, all configurable: Run wall-clock timeout, per-Sub-task timeout, per-fetch timeout (also applied to Fact-Checker re-fetches), LLM token cap per Run, `task` recursion depth. A Sub-task limit fails the Sub-task. A Run limit fails the Run with the reason in the Run log.
- Runtime estimate shown before submit is `base + n_pdfs x ingest_cost + per-source costs`, refined live once the real Sub-task count arrives.

## 11. Package layout and execution

```
src/insightforge_agent/
  domain/      typed models and pipeline state, no I/O
  pipeline/    LangGraph graph, one module per stage
  agents/      prompts and DeepAgents definitions
  stores/      SourceStore, vector wrapper, repositories
  api/         FastAPI
  scheduler/   APScheduler, Diff and retention sweep
```

Dependencies point inward, so `domain` imports nothing from the other packages. An import-linter check enforces it. Runs execute in the API process behind a `RunExecutor` interface, with a concurrency cap and a startup sweep:

- A Run left `RUNNING` by a crash is marked `FAILED`.
- A Run left `RESUMING` is resumed again from its checkpoint with its stored resolution, which is idempotent.
- A Run in `AWAITING_CLARIFICATION` whose deadline passed while the process was down is resolved as a timeout. One still within its deadline is left alone.

An `insightforge-agent eval` command runs the 5 sample briefs and writes the evaluation report. Fact-check pass rate and citation accuracy are deterministic, and synthesis quality uses an LLM judge with a fixed rubric.

## 12. Facts to verify at build time

These are library facts the design relies on. An external review reports the first group as confirmed against current releases. Each is still re-checked against the installed packages in the slice that first needs it.

Reported as confirmed by the review:

- `deepagents` subagents are isolated by default, and `create_deep_agent` includes automatic summarization.
- `deepagents` supports Python 3.12 (`.python-version`).
- LangGraph supports durable interrupt and resume with a checkpointer, and a SQLite checkpointer exists (section 2).
- Docling's chunk metadata exposes heading breadcrumbs and page numbers for the `doc_chunks` metadata.

Still open:

- **Spike (first ticket, blocks every research-pipeline ticket):** one checkpointed Planner thread resumed across two outer LangGraph nodes (ADR-0001, ADR-0003). One minimal executable integration test, with a fake researcher tool and a real model, passes when:
  1. `PLANNING` produces a `SubTaskPlan` on a checkpointed thread and the outer graph advances.
  2. `RESEARCHING` resumes the same thread with the approved Sub-tasks intact and emits one `task` per Sub-task.
  3. Every Sub-task ID gets a result, and a deliberately dropped Sub-task is recovered by the repair round.
  4. Over 5 runs, the share of runs in which all `task` calls arrive in one turn is recorded. This is measured, not required.

  If 1 to 3 fail, the fallback is two invocations of the same Planner definition: `RESEARCHING` starts a fresh Planner seeded only with the approved `SubTaskPlan`. The Planner root agent still spawns the Researchers via `task`, so this stays compliant, and only ADR-0001's "one thread" wording changes. Scheduling, API and persistence tickets do not wait for the spike.
- Whether `write_todos` / `TodoListMiddleware` is on by default or must be passed explicitly.
- Qdrant can create and delete a temporary collection cheaply enough where one is needed, or a payload-scoped alternative is used instead.
- Token counting for the Evidence budget and the per-call cap matches the provider's tokenizer closely enough for a deterministic test.
