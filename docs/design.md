# InsightForge technical design

Status: confirmed. Vocabulary is defined in `../CONTEXT.md`; decisions that are hard to reverse are in `adr/`. Source spec: `../assignment_04_market_intelligence.md`.

## 1. Scope and users

- Implementation sequence: foundation, then the spike (section 12), then the research happy path, then the API and UI, then the remaining capabilities (scheduling and diffing, and the rest).
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
  - **Bounds.** The plan must hold 3 to 6 Sub-tasks, each referencing exactly one Aspect, and every Aspect covered by at least one Sub-task (an Aspect may need several, for example with different source types). A plan outside the bounds, or with more than 6 Aspects, gets exactly one corrective re-prompt that states the violation. For too many Aspects, the re-prompt asks the Planner to consolidate genuinely related Aspects, never to drop one.
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

- **Scratch store.** Sources, Observations and Passages live in our database behind a `SourceStore` interface. `read_source(source_id)` is a thin tool over it. PDF chunk text lives in Qdrant, and the same interface resolves those IDs. Source IDs are content-derived and immutable within a Run. For a web Source the ID is derived from its URL, not from the page body, so a page that changes stays one Source and gains Observations; for an uploaded PDF it is derived from the file's content hash. What a fetch contained is identified one level down: the Observation ID hashes the body together with the Source, the User and the fetch time.
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
  | Memory researcher | None. It sees references to earlier Reports' Claims, with their dates and headings |
  | Writer | None. It reads only the Evidence bundle and the Historical context (earlier Claims, never Passages) |
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
- **Extractor.** Works one Source at a time in Passage batches. It persists entities and structured facts with their Passage references to the SQLite `entities` table as it goes. Conflicts are then detected over the structured facts grouped by entity, never over raw text, and marked `conflict: true`.
- **Synthesis.** Works one Sub-task at a time over that Sub-task's Passages and drafts Evidence items, each with its supporting Passage IDs, `as_of_period` and scope. After all Sub-tasks are done, it deduplicates and merges over the structured items, then ranks and compresses to budget.
  - **Validation.** Every candidate Passage first passes a deterministic provenance check and a literal-value check (every number and named entity in the item appears in the Passage). Then one bounded entailment call per Evidence item judges its candidates as `SUPPORTED`, `CONTRADICTED` or `INSUFFICIENT_EVIDENCE`. The call is chunked if the candidates exceed the per-call cap and counts against the Run's token cap.
  - **Confidence and primary Passage.** Only `SUPPORTED` Passages count toward Confidence. The primary Passage is the supported one with the highest Source credibility, then the highest relevance to the item, then the lowest Passage ID. An item with no supported Passage is dropped and logged.
  - **Contradictions.** A `CONTRADICTED` Passage triggers a context check. If it concerns the same entity, predicate and scope with an overlapping As-of period, the item is marked conflicting and its Confidence is `LOW`, whatever the Sources' independence. The item links the conflicting Extraction facts and keeps both the supporting and contradicting Passage references. Non-overlapping periods or different scopes are distinct facts.
  - **As-of period.** A period the Passage states is used as is. If a Source has neither a stated period nor a publication date and the Passage asserts a present state, `as_of_period` is the Observation's date with `derived_from = observation_date`. This applies only to present-state assertions, not to history, predictions or announcements. Otherwise the period is `UNKNOWN` and flagged as temporally ambiguous. Every derived period is logged. The present-state judgement is part of the entailment call.
  - **Ranking.** Evidence items are sorted by the tuple `(recency_bucket desc, credibility_bucket desc, relevance_to_brief desc, subtask_priority asc, evidence_id asc)`. Relevance is cosine similarity between the item and the Brief. Bucket thresholds are settings. Ranking never changes provenance, independence or Confidence.
- **Writer.** Produces the Report with the required sections. Every Claim cites an existing Evidence item, and its inline citation shows the item's primary Passage; a Historical Claim instead cites a Historical item (an earlier Claim that keeps its original Passages), never an Evidence item. Every number and named entity must appear in the item. Historical Claims show their original date inline. Violations trigger up to 2 retries with the offending Claims listed as a constraint.
- **Fact-Checker.** Samples 20% of Claims (minimum 5), seeded from the Run ID. A Report with fewer than 5 Claims has all of them checked. Historical Claims are eligible and are checked against their original Sources. Verdicts are `verified | unverified | unchecked` (section 7).
- **Diff Agent.** Compares Findings against the Baseline (section 8).

## 6. Persistence

- **Database (SQLite behind repository interfaces).** Users' Runs, Sub-tasks, Sources, Observations, Passages, Reports, Watchlist Items, Baselines, the Diff log, Alerts, `run_events`, failure records, revoked share links, and the APScheduler job store.
- **Qdrant.** `doc_chunks` and `report_sections` (prior Reports). Extracted entities and facts are in the SQLite `entities` table; there is no Qdrant `entities` collection. One configurable embedding model, with its name recorded in each collection's metadata. Every query is filtered by `owner_id` inside the repository. `doc_chunks` is Run-scoped for visibility only: a Run searches only its own uploads, but chunks are kept while a Report cites them.
- **Progress.** The pipeline appends to `run_events`. The SSE endpoint tails it with a cursor (`Last-Event-ID`), so a reconnecting client loses nothing. Retention later removes an expired Run's detailed events and keeps its stage trail and failure events (T13 notes).
- **Retention.** A periodic sweep removes material based on what still refers to it, not on the originating Run's status.
  - **Protected material.** A Source, Observation, Passage or document chunk cited by a Report, or by the evidence behind a Baseline Finding, is never removed.
  - **Failed Runs.** Their raw material expires after a configurable retention period. A small failure record survives it: the reason, timestamps, the stage, and error codes. Failed Run material never reaches the memory researcher.
  - **Completed Runs.** Material from a completed Run that nothing retained refers to becomes eligible for removal after the same period.
  - **Entities.** An extracted entity whose underlying Passage is removed keeps the entity with its provenance marked unavailable, never a dangling reference.
  - **Implementation.** The cutoff, the protected records, the failure record and the cross-store crash behaviour are in the T13 notes.

## 7. Credibility, Confidence and fact-checking

- **Credibility score** (0 to 1) exists on every Source: web by domain type, recency decay and byline; uploaded documents by document type and date; memory Sources inherit from the Sources behind them.
- **Confidence** belongs to an Evidence item and counts only its `SUPPORTED` Passages:
  - `HIGH` needs 2 or more Independent Sources, counted over distinct Sources, never Passages.
  - `MEDIUM` is a single high-credibility Source.
  - `LOW` is a single low-credibility Source or a conflicting item (section 5).

  A section's label is the lowest among its items.
- **Fact-check.**
  - **Re-fetch.** Re-fetching a web Source creates a new Observation with its own Passages. The original Observation, its Passages and the Claim's citation are unchanged.
  - **Verdict** (revised by F2, owner-approved). The Passages are cut into spans: each sentence and each window of two adjacent sentences (as many as the Claim has, up to four, for a longer Claim). The embedder scores the Claim against every span; spans under 0.85 (configurable) are dropped, and so are spans that do not state every figure of the Claim. The best 3 remaining spans go to one light-model entailment call (SUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE, as in Synthesis). The Claim is verified only if a span is judged SUPPORTED. With no span left there is no model call. A failed, invalid or unusable reply leaves the Claim `unverified`, never `unchecked`. The verdict references the parent Passage of the supporting span (else of the best span).
  - **Pass rate.** `unchecked` (the re-fetch failed) is excluded from the pass rate.
- **Non-web Sources.** For uploaded documents, run the same max-over-Passages check against the stored Passages of the cited document, which are the original text and not the Evidence item. For `memory` Sources and Historical Claims, follow the inheritance chain to the original Sources and check those, re-fetching if they were web pages. The verdict records which kind of check ran.

## 8. Scheduling and diffing

- **Ticks.** APScheduler runs one Run per Watchlist Item per tick, using the same pipeline. At most one active Run per item. Overlapping and missed ticks are skipped.
- **Run limit.** A Watchlist Item has an optional `max_runs`. `max_runs` counts Runs started, and a Failed Run still consumes one slot. Once that many Runs have started, the item stops scheduling and the dashboard shows "N of N runs started", with the completed and failed counts shown separately. This is a field, not a status. A cadence under 15 minutes is accepted only when `max_runs` is set, so a demo item cannot spend provider calls indefinitely.
- **Watchlist sources.** A Watchlist Item always has web search enabled and can never use uploaded documents. It may add Prior Reports for background. Both rules are checked at creation.
- **No Watchlist status.** Because web search is always enabled and always available, a tick can never lack a source, so a Watchlist Item has no `blocked` state and there is no per-tick source check. The app refuses to start when no configured search provider is usable (section 10). A provider outage during a Run ends in a Failed Run, which raises no Alert and leaves the Baseline unchanged. The dashboard shows each item's recent Failed Runs from Run data.
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
- The citation drawer resolves a Claim's Evidence item to its primary Passage and shows the Source title, publication date, credibility score and the Passage text, for every Claim, independently of the Fact-Checker's sample. Served by `GET /reports/{report_id}/citations` (`list[Citation]`, from `pipeline.citations.report_citations`); provenance that does not resolve is 500 (`ProvenanceError`), never a partial list. There is no general Source or Passage endpoint.
- Export: Markdown, PDF, and the share link. PDF is rendered with Playwright's headless Chromium from the same HTML the viewer uses, behind an interface so another renderer can replace it. The README notes the one-time `playwright install chromium`.

## 10. Configuration and limits

One typed settings object (pydantic-settings, from the environment and `.env`). The app refuses to start when a required secret for the chosen LLM provider is missing, or when no configured search provider is usable (see Search). `.env.example` is committed and `.env` is ignored.

- **Models.** `LLM_PROVIDER` (`anthropic | gemini | groq`) and a model name per role (Planner and Writer; a cheaper one for Researcher summaries and entailment calls). One `build_chat_model(role)` factory is the only code that knows about providers. Development and dev tests use the Gemini free tier (Groq is selectable). Anthropic is used only for the final verification run (ticket 18).
- **Embeddings.** A local `fastembed` model (default `BAAI/bge-small-en-v1.5`) in every environment, so embeddings do not change with the chat provider.
- **Search.** An ordered list, `SEARCH_PROVIDERS=tavily,serpapi` (`brave` also allowed). A provider that errors or has exhausted its quota falls through to the next. A Run fails on search only when every listed provider fails.
  - **Startup validation.** At startup at least one listed search provider must be usable, meaning it has its credentials. A missing key for an optional fallback does not stop the app while another listed provider is usable, and that provider is logged as skipped. If every listed provider lacks credentials, the app refuses to start.
- **Tests.** The default `pytest` run uses fake or scripted chat models, recorded search fixtures and in-memory Qdrant, so it needs no keys or Docker. Tests that need a real model or server carry a `live` marker and run on demand. Dev uses `docker compose` for a Qdrant server.
- Provider keys, Qdrant URL, database URL, personas file path.
- Evidence token budget 6,000, per-call Passage token cap, Sub-tasks 3 to 6, Fact-Checker rate 20% (minimum 5) and similarity threshold 0.85, recency and credibility bucket thresholds, clarification timeout 30 minutes, Not reconfirmed display threshold 3, retention period (`RETENTION_DAYS`, default 30), share-link TTL 7 days, JWT secret.
- Limits, all configurable: Run wall-clock timeout, per-Sub-task timeout, per-fetch timeout (also applied to Fact-Checker re-fetches), LLM token cap per Run, `task` recursion depth. A Sub-task limit fails the Sub-task. A Run limit fails the Run with the reason in the Run log.
- Runtime estimate shown before submit is `base + n_pdfs x ingest_cost + per-source costs`. Implemented as `POST /research/estimate` (`domain/runtime_estimate.py`) with `RUNTIME_ESTIMATE_BASE_SECONDS` 30, `_PDF_INGEST_SECONDS` 15, `_WEB_SECONDS` 45, `_DOCUMENTS_SECONDS` 30, `_MEMORY_SECONDS` 15; omitted toggles follow submission's persona-default rule (`effective_source_toggles`). A heuristic, not a promise: it checks no availability and creates or reserves nothing. The pre-submit estimate is implemented and shown by the T14 UI. Live refinement would require a post-planning progress event carrying the Sub-task count and is deferred.

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
- A Run in `AWAITING_CLARIFICATION` whose deadline passed while the process was down is resolved as a timeout. One still within its deadline is left waiting, and its timeout job is scheduled again from the stored deadline.

An `insightforge-agent eval` command runs the 5 sample briefs and writes the evaluation report. Fact-check pass rate and citation accuracy are deterministic, and synthesis quality uses an LLM judge with a fixed rubric. The sample brief library (5 briefs across industries and question types, each with its expected report structure) is part of the eval ticket. Results are recorded per provider. A fact-check pass rate under 80% on the Anthropic run triggers a documented remediation attempt.

## 12. Facts to verify at build time

These are library facts the design relies on. An external review reports the first group as confirmed against current releases. Each is still re-checked against the installed packages in the slice that first needs it.

Reported as confirmed by the review:

- `deepagents` subagents are isolated by default, and `create_deep_agent` includes automatic summarization.
- `deepagents` supports Python 3.12 (`.python-version`).
- LangGraph supports durable interrupt and resume with a checkpointer, and a SQLite checkpointer exists (section 2).
- Docling's chunk metadata exposes heading breadcrumbs and page numbers for the `doc_chunks` metadata.

Still open:

- **Spike (ticket 1, after the minimal scaffold in ticket 0; blocks every research-pipeline ticket):** it first verifies the library facts below against the installed versions, then proves the integration. Verification comes first and covers `write_todos` / `TodoListMiddleware` defaults, checkpointing, interrupt and resume, and subagent isolation. Library versions, findings and references are recorded here and in the design rationale. The proof uses only verified APIs and applies the fallback if checkpoint continuity fails. It runs on the dev provider and is re-run on Anthropic in ticket 18, where criteria 1 to 3 must also pass. One checkpointed Planner thread resumed across two outer LangGraph nodes (ADR-0001, ADR-0003). One minimal executable integration test (marked `live`), with a fake researcher tool and a real model, passes when:
  1. `PLANNING` produces a `SubTaskPlan` on a checkpointed thread and the outer graph advances.
  2. `RESEARCHING` resumes the same thread with the approved Sub-tasks intact and emits one `task` per Sub-task.
  3. Every Sub-task ID gets a result, and a deliberately dropped Sub-task is recovered by the repair round.
  4. Over 5 runs, the share of runs in which all `task` calls arrive in one turn is recorded. This is measured, not required.

  If 1 to 3 fail, the fallback is two invocations of the same Planner definition: `RESEARCHING` starts a fresh Planner seeded only with the approved `SubTaskPlan`. The Planner root agent still spawns the Researchers via `task`, so this stays compliant, and only ADR-0001's "one thread" wording changes. Scheduling, API and persistence tickets do not wait for the spike.
- ~~Whether `write_todos` / `TodoListMiddleware` is on by default or must be passed explicitly.~~ Resolved by the spike: it must be passed explicitly (see spike results).
- Qdrant can create and delete a temporary collection cheaply enough where one is needed, or a payload-scoped alternative is used instead.
- Token counting for the Evidence budget and the per-call cap matches the provider's tokenizer closely enough for a deterministic test.

### Spike results (ticket 1, 2026-10-08)

Installed: `deepagents` 0.7.23, `langgraph` 1.2.14, `langgraph-checkpoint-sqlite` 3.1.1, `langchain` 1.4.3. Final run on Anthropic `claude-haiku-5-5` for every role (at the user's request, because the Gemini free tier hit its 20 requests/day cap on `gemini-3.5-flash` and Groq's 8,000 tokens/minute cap rejected larger requests). Earlier partial runs on Gemini and Groq reached the same plan and research steps. Harness: `tests/live/planner_spike.py`; live tests: `pytest -m live tests/live`.

Verified:

- **`write_todos` is not on by default.** `create_deep_agent(model=...)` exposes `ls, read_file, write_file, edit_file, delete, glob, grep, execute, task`. `TodoListMiddleware` is only added by the OpenAI Codex harness profile. Pass `middleware=[TodoListMiddleware()]` explicitly. This resolves the open question below.
- **Subagents are isolated.** `SubAgent.mode` defaults to `isolated`, and `messages` and `todos` are excluded from state passed to and returned from a subagent. Only the final message returns, as the `task` ToolMessage. `create_deep_agent` also adds `SummarizationMiddleware` (per its docstring; not exercised by the spike).
- **Checkpointing, interrupt and resume.** A SQLite checkpointer persists the Planner thread across outer graph nodes. A LangGraph `interrupt()` resumes with `Command(resume=...)` from a new graph instance on the same SQLite file (`tests/test_interrupt_resume.py`).
- **Criteria 1 to 3 pass** on Haiku 5.5 (`pytest -m live tests/live -k "not parallel"`, one run, 3 passed in 25 s; no log is committed). The checks are pure functions in `tests/live/spike_checks.py` with deterministic tests, and the whole graph also runs with scripted models and no API calls (`tests/test_planner_spike_scripted.py`):
  - **One-to-one dispatch.** The initial `task` calls match the approved Sub-task IDs exactly (none missing, duplicated or invented), and each description contains the approved query verbatim.
  - **Plan continuity.** The plan held by the graph equals the plan submitted in the thread, and the `RESEARCH` message contains no plan text, so the dispatches can only come from the checkpointed thread.
  - **Repair.** The missing Sub-task's result is credited only from a `task` call after the `REPAIR` message, with a `tool_call_id` that differs from every original dispatch. Repair dispatches only the missing ID with its original scope. A test where the original result stays in history and repair does nothing leaves the Sub-task `missing`. An explicit `FAILED:` result is `failed`, is final and is never retried, and a later or duplicate result never overwrites a success.
  - **Isolation.** A scripted Researcher records everything it receives: only its own task description, never the Brief, the plan or other Sub-tasks' queries.
  - In the live run the drop is simulated (the verifier ignores one research result). The scripted tests make the Planner genuinely omit a Sub-task.
  The fallback is not needed, and ADR-0001 stands.

Findings that change how later tickets build on this:

- **Do not use `response_format` on the Planner.** It forces a structured-output call on every invocation of the thread, so the RESEARCH and REPAIR resumes break (`Tool choice is required` on Groq; JSON mode cannot be combined with tools on Groq). The spike has the Planner call a `submit_plan(plan: SubTaskPlan)` tool in `PLANNING`, and the graph reads the plan from that tool call.
- **Disable thinking when resuming the Planner thread on Anthropic.** Haiku 5.5 returns signed thinking blocks by default, and deepagents rewrites earlier messages in the thread, so the repair resume fails with `Invalid signature in thinking block`. The spike sets `thinking={"type": "disabled"}` on the model. The production model factory does not do this yet; tickets that resume the Planner on Anthropic must.
- **Token footprint.** The deepagents system prompt plus the accumulated thread is large: on Groq's free tier (8,000 tokens/minute) a repair request of 8,172 tokens was rejected.
- **Plan model bounds are not enforced yet.** `SubTaskPlan` checks only that Sub-tasks reference known Aspects. The 3 to 6 bound and Aspect coverage belong to the Planner-bounds ticket (T4), which needs invalid plans to stay representable for the corrective re-prompt.

Criterion 4 (measured, not required): over 5 runs on Haiku 5.5, all `task` calls arrived in a single turn in **5 of 5** runs (`test_parallel_dispatch_share_over_5_runs`). This is one small model and one Brief, so it is not a guarantee, which is why the graph still verifies results (ADR-0003). Re-run criteria 1 to 3 in ticket 18 as planned.

## 13. Frontend

- The Next.js 14+ (App Router) app lives in `web/` in the same repository.
- A Next.js `rewrites` rule proxies `/api/*` to FastAPI, so the browser sees one origin. This keeps the signed persona cookie and the SSE `EventSource` (which cannot set headers) working, and needs no CORS.
- Dev runs `uvicorn` and `next dev` locally, with `docker compose` for Qdrant only.
- Scope: brief input, live SSE progress, report viewer with citation drawer, watchlist dashboard, alerts, and export. Bonus items (including click-any-sentence citation trace and LangSmith tracing) are out of scope unless fully built and demonstrated.

### T3 notes: the thin Brief to Report path

Built as a tracer bullet (`pipeline/graph.py`, `run_brief`). Minimal forms that later tickets thicken:

- **Fact-check:** a pass-through in T3, implemented in T6 (see the T6 notes below).
- **Since then:** `INGESTING` and documents arrived with T7, memory with T8, clarification with T9.
- **Extraction and synthesis:** thin forms in T3, replaced in T5 (see the T5 notes below).
- **Per-call cap:** see the T5 notes (oversized Passages are read in windows). The Researcher summary reads the lead Passages that fit the cap, and logs `summary_input_limited` when it does not read the whole page.
- **Researcher refs:** the Researcher names Source IDs in its `RESULT:` reply. The fetch tool records `{source_id, title, summary}` as a durable `source_summarized` run event, and the graph rebuilds `ResearchResults` from that log, so a restart mid-Run loses nothing.
- **Search:** only Tavily is implemented; other listed providers are skipped with a warning.
- **State:** the outer graph is checkpointed under its own thread, apart from the Planner's, and `complete` reuses an existing Report for the Run so a retry never inserts a second one. State holds the plan, extracted statements, Evidence bundle and draft, which are short sentences and not Source bodies.

### T4 notes: Planner bounds and the Submission check

- **Rules are pure** (`domain/plan.py`: `plan_violations`, `trim_plan`; `domain/submission.py`). The graph's `planning` node applies them: one corrective `PLAN (correction)` re-prompt on any violation, then trim to 6 by priority (never the last Sub-task of an Aspect), then fail with the violations as the reason. Logged as `plan_correction` and `plan_trimmed`.
- **Submission check:** `run_brief` raises `SubmissionRejected` before a Run exists; the API (T10) maps it to 422. Memory counts as available when the User has an eligible Report (defined by T8, see the T8 notes).
- **Runnable types:** `run_brief` also rejects (`SubmissionRejected`, no Run) a Brief whose available types a Run cannot carry out yet (`web` and, since T7, `documents` when the chunker and `doc_chunks` index are wired), and, since T8, `memory` when the `report_sections` index is wired. Web plus unavailable documents still runs on web.
- **Ids:** Aspect ids must be unique and non-empty; Sub-task ids match `SUBTASK_ID_PATTERN`, the same pattern dispatch parsing uses, so an accepted id always round-trips.
- **Dispatch guard:** `ApprovedDispatchGuard` (Planner middleware) refuses at runtime any `task` call that is not an approved Sub-task whose description is exactly `canonical_description(id, query)` and whose `subagent_type` is `researcher`: none are approved during PLAN, all during RESEARCH, only the missing ones during REPAIR. Refusals are logged as `dispatch_rejected`. The Planner can still attempt a bad call; it is stopped before a Researcher runs.
- **Not done:** corrective attempts do not yet count against a Run-wide LLM token cap (no such cap exists yet).

### T5 notes: Extraction and synthesis

- **Extraction** (`pipeline/extract.py`): one light-model call per Passage batch, one Source at a time. Each fact carries `entity, predicate, value, scope, period` plus its Passage reference and is stored in the `entities` repository (`EntityRepository`, SQLite or in memory; it stays in SQLite, there is no Qdrant collection for it) after each Source. Conflict detection (`domain/extraction.py`) groups facts by normalised entity, predicate and scope and flags different values for overlapping periods, with no model call. An UNKNOWN or unreadable period (`domain/periods.py`) overlaps nothing.
- **Synthesis** (`pipeline/synthesize.py`): draft per Sub-task in Passage batches, merge candidates with the same structured key (the most important Sub-task keeps the item; since F4 an UNKNOWN-period key also names the Observations cited, see below), then validate each item. Provenance gates every candidate. The literal-value check (every number, declared entity and capitalised non-initial word must appear in the Passage) decides which Passages may *support* an item; one that fails it can still be judged `CONTRADICTED`. One entailment call per item, chunked at the per-call cap. A `SUPPORTED` verdict on a Passage that failed the literal check is ignored.
- **Contradictions** need Extraction facts at the contradicting Passage with the item's entity, predicate and scope and an overlapping period; otherwise the Passage is logged as `contradiction_ignored`. An item with no structured entity/predicate therefore never becomes conflicting.
- **As-of period:** stated, else derived from the Observation date for a present-state assertion from a Source with no publication date (`derived_from = observation_date`, logged as `as_of_derived`), else `UNKNOWN` with `temporally_ambiguous`. A Source that has a publication date but states no period is `UNKNOWN`, as the spec words it.
- **Independence:** different domains, and neither Source's Passage text mentions the other's address or domain (`domain/evidence.py`). A real citation graph is not stored.
- **Ranking and budget:** `rank_key` implements the specified tuple; thresholds live in `Settings` (`credibility_*`, `recency_*_days`) and reach the pipeline as `Deps.thresholds`. Relevance uses the `Embedder` interface (`embeddings.py`); the default is a dependency-free hashing embedder, and the local `fastembed` model replaces it in T7/T8. Compression cuts whole lowest-ranked items until the rendered bundle fits, keeping the last item of an Aspect for as long as another can go; each cut is an `evidence_cut` event with reason, Confidence and rank.
- **Token cap:** `Deps.spend` counts the estimated input tokens of every Passage-reading call against `run_token_cap` (default none) and fails the Run when it is exceeded. Planner and Writer calls are not counted yet.
- **Model roles:** drafting and entailment use the light model for now.
- **Hard Passage cap (review round):** `batch_passages` returns `Window`s. A Passage over the cap is read in several temporary windows (marked "part n of m" in the prompt), so no call's Passage content exceeds the cap. A window is never stored: the Observation is not re-split, and facts, verdicts and citations still name the whole Passage. A verdict is combined per Passage (any window SUPPORTED supports it), so a statement that spans two windows cannot be verified.
- **Cross-batch contradictions:** besides the Passages a draft cites, the entailment call for an item also gets "challenger" Passages: Extraction facts with the same entity, predicate and scope, a different structured value and no clearly different period. Challengers can never support an item. A contradiction counts only when a `CONTRADICTED` verdict meets such a fact at that Passage with an overlapping period (using the final, possibly derived, period); a verdict over an equal value is logged as `contradiction_ignored`.
- **Shared Sources:** Sources are de-duplicated within a Sub-task only; a Source several Sub-tasks found is drafted for each, and equivalent items still merge into the most important Sub-task.
- **Model-supplied structure is verified:** a structured `value` must be stated in the Passage (`value_in_text`) and a stated period must be supported by it (`period_stated_in`: the year and any quarter, half or month). Otherwise Extraction clears the value (`extraction_value_rejected`) or sets the period to UNKNOWN, and Synthesis does the same (`value_rejected`, `period_rejected`; since F4 a drafted period is also checked against its cited windows before Merge, `draft_period_rejected`). A rejected period falls through to the observation-date derivation rules. The check is lexical: it does not prove the value belongs to the stated entity.
- **Cap accounting (review round 2):** the per-call cap is measured on the rendered Passage portion of the payload (`render_batch`: labels, part markers and separators), not on raw text. A batch is only extended while its rendering still fits, and a Passage that cannot fit alone as rendered is windowed against that same measure. System instructions and the one-line header (Sub-task, statement, page title) are outside the cap.
- **Value matching:** `value_in_text` matches the whole value as one run of numbers and words (million vs billion, percent and currency signs read as unit words); abbreviations such as "4.27m" are deliberately not matched. `period_stated_in` requires the day of a full date next to its month ("September 14", "14th of September") or the ISO date itself.
- **Confidence observability (final round):** each validated item logs one `confidence_decided` event before the budget cut: `evidence_id`, `confidence`, `decision_rule` (`conflicting`, `independent_sources`, `high_credibility_single_source` or `insufficient_credibility`), `supported_passage_count`, `distinct_supporting_source_count`, `independent_pair_found`, `best_source_credibility`, `credibility_high_threshold`, `conflicting` and `source_ids`. No Passage text. Scores, thresholds and rules are unchanged; `tests/domain/test_confidence_matrix.py` documents today's Source-score/Confidence matrix. See the F1 credibility notes below for the current scores.
- **Numeric canonicalization (`domain/quantities.py`):** two representations of one quantity are the same value only when their exact `Decimal` values are equal and any stated unit agrees ("4.27 million" = "4,270,000"; "$32,000" = "32,000 dollars"; "20%" = "20 percent"). It feeds the merge key, structured-value matching, the item-level number check and conflict detection, so equivalent formats neither fail to corroborate nor look like conflicts. The statement and Passage text are never rewritten, and every supporting Passage still has to state the surviving statement. Same-written figures keep the earlier leniency on units (the entailment call judges what is measured); a different representation needs the claimed unit to match. "4.27 million" and "4.27 billion" now differ in the number check too.
- **Not equated, deferred to follow-up:** qualifiers ("about 4.27 million"), rounded vs exact values, abbreviations ("4.27m"), predicate synonyms and entity aliases (needs the T12 vocabulary), period containment (2025 vs 2025-12) and citation-graph independence. (Publication-date extraction and the credibility rubric arrived with F1, below.)

### F1 notes: web Source metadata and credibility

- **Publication date** (`agents/web.html_to_page`): JSON-LD `datePublished` from Article types (and subtypes), then from WebPage types, at any nesting (`@graph`, `mainEntity`, lists); other objects such as ImageObject are ignored. Then `article:published_time`, then `itemprop="datePublished"`. ISO 8601 only; a date or naive time is read as UTC. A value that is missing, unreadable, before 1990 or after the fetch time gives `None`. Nothing is taken from a bare `date` meta tag, `dateModified`, `<time>` elements, body text or the URL.
- **Byline:** JSON-LD `author` (a string, `{name}` or a list; an Organization does not count), then `author` / `article:author` meta. Only its presence is scored; it is not stored. A site that puts its own name in `author` meta still earns it (+0.05, never enough for MEDIUM alone).
- **Credibility** (`agents/web.credibility_score`), additive and rounded to 2 places: domain .gov/.edu/.int 0.8, major press (`MAJOR_PRESS`, a short allow-list matching the domain and its subdomains) 0.7, any other 0.55; recency +0.1 up to a year, -0.1 over three years, 0 otherwise or when the date is unknown; byline +0.05. An undated unknown domain stays below MEDIUM even with a byline; a dated recent one, or major press, reaches MEDIUM alone. The Confidence policy and thresholds are unchanged; `tests/domain/test_confidence_matrix.py` holds the matrix.
- **Knock-on:** a Source with a publication date no longer has a present-state As-of derived from the Observation date (section 5 rule, unchanged).
- **Limitation:** a Source row is written once (first ingest wins), so a Source stored before F1 keeps its old score and no date, and a later fetch or Fact-Checker re-fetch does not update it. Use a fresh database for evaluation runs. Plausibility is checked against the wall clock at parse time, the score against the Run's clock; they differ only with an injected clock.

### F2 notes: Fact-Checker verification (owner-approved policy change)

- **Why.** With the production `bge-small` embedder, similarity cannot separate a correct paraphrase (0.961) from a wrong-measure Claim with the right figures (0.963; g-ma-02 0.948), and whole-paragraph matching rejected true Claims (0.81-0.84). The owner approved span matching plus an entailment judgment instead of any threshold change (`docs/audits/2026-10-11-f2-fact-checker.md`, ledger row 13).
- **Roles.** Similarity finds relevant text; the numeric check requires the figures; the light-model entailment call judges support. Threshold 0.85, sampling, `[UNVERIFIED]`, and the re-fetch versus `document_passages` split are unchanged.
- **Spans** (`domain/passages.candidate_spans`, `sentences_of`): every sentence (split at `.`, `!` or `?` only before a word whose first letter is upper case, so "approx. 40%" stays whole; no abbreviation list, so "...than Kia. Hyundai..." splits, and a name cut at an abbreviation, "BYD Co. | Ltd. sold...", is whole again in a window), each followed by its windows with the next sentences. Windows are two sentences wide, or as wide as the Claim when it has more sentences, up to four (`window_width`): a Claim of several sentences could otherwise never find all its figures in one span (seen in the T7 real-PDF test). A sentence over 600 characters is cut at whitespace. A Passage of n sentences gives 2n - 1 spans for a one- or two-sentence Claim. A span is never stored; it is cited as its parent Passage.
- **Entailment** (`pipeline/fact_check._entailed`): at most `TOP_K` = 3 spans in one call per sampled Claim, using Synthesis's prompt and schema (`pipeline/entailment.py`, shared, moved unchanged) and `render_batch`. Each group of overlapping spans takes one place, so a sentence and its own windows cannot fill every place. Within a group, a wider span that contains the best one and also passes the gates replaces it, so the judge sees the context: "It sold..." together with the sentence that names the subject. Groups enter the top 3 by their ranking score: the best qualifying similarity that established the group, so widening never lowers a group's place. The judge sees the representative, which must itself pass both gates. A verdict reports that representative's own similarity, never the ranking score. The whole request, system prompt included, never exceeds `passage_token_cap`. The weakest candidates are dropped until it fits. If even the best one alone does not fit, no call is made and the Claim is `unverified` (`fact_check_entailment_skipped` is logged). Only an unambiguous SUPPORTED counts. Any ordinary exception from the model path (`Exception`, `AssertionError` included), invalid JSON, or verdicts that name no shown span leave the Claim `unverified` (`fact_check_entailment_failed` is logged for failures, `fact_check_entailment` for replies). `KeyboardInterrupt` and `SystemExit` still propagate. Token use counts against `run_token_cap` like any Passage-reading call: when a cap is configured (none by default), exceeding it fails the Run here as it would in Synthesis.
- **Embedding** (`pipeline/fact_check.Vectors`): each distinct text is embedded once per fact-check, so a Source cited by several sampled Claims is not embedded again.
- **Cost.** At most one light call per sampled Claim, and none when no span passes the gates: about 5-8 calls for a typical Report (20% of Claims, minimum 5).
- **Benchmark baseline.** It makes no model call, so a CLAIM_PASSAGE case whose spans pass the gates is now NOT_APPLICABLE (as SUPPORT is); one the gates reject is still NOT_GROUNDED, from the real gates (`fact_check.screen`) (`benchmark/baseline.py`, version `m1.3-2`; `m1.3-3` since F4).
- **Tests.** `tests/domain/test_spans.py`, `tests/pipeline/test_f2_fact_check_entailment.py` (scripted judge, in CI) and `tests/integration/test_f2_fact_check_bge.py` (real bge-small retrieval with an oracle judge; `-m integration`). `tests/live/test_f2_real_model.py` (paid; `-m live` plus `INSIGHTFORGE_ALLOW_LIVE_TESTS=1`) runs the same 16 cases with the production light model; it passed 16/16 with `claude-haiku-5-5` on 2026-10-12, and F2 is closed.

### F4 notes: no merge across Observations on an UNKNOWN period

- **Rule** (`pipeline/synthesize.merge_identity`). A structured candidate whose stated period is UNKNOWN (none given, or unreadable such as "this year" or "fiscal 2023") is also keyed by the sorted set of Observation IDs it cites. It merges only with candidates citing exactly that set. Explicit periods and statement-keyed candidates are keyed as before. Observation IDs are content-derived, so Evidence IDs stay deterministic.
- **Why.** Two undated Observations of one value (a headcount in 2021 and in 2025, "this year" in 2023 and in 2024) were one item supported by two Sources, so HIGH. Where the statements differed, the literal check removed the second Passage, and its fact was lost without a log.
- **Kept.** Duplicates from one Observation (several Passages, other Sub-tasks, an unreadable period) still merge. A candidate the drafting model gave several Observations is kept whole and judged against all of them. Overlapping but unequal sets stay separate items: this can duplicate, but never merges falsely.
- **Period checked before Merge.** An explicit period the model gives is used in the key only if at least one cited window states it (`period_stated_in`, the same rule as validation, on exactly the text the model was shown). Otherwise it is UNKNOWN before Merge, so the provenance rule applies, and `draft_period_rejected` is logged (`evidence_id`, `subtask_id`, `period`). Validation still checks the period against the supporting Passages.
- **Not addressed.** Same-name different entities with an explicit period (`p-fm-02`) and the drafting model's own grouping of undated Observations, both on the T16 watchlist. Also statement-keyed candidates, and a period stated only in a window of an over-cap Passage the model was not shown (dropped before Merge: lost recall, never a false merge). Details: `docs/audits/2026-10-12-f4-false-merge.md`.

### T7 notes: Document ingestion and Doc Reader

- **Uploads** (`domain/documents.py`): `run_brief(..., uploads=[Upload(filename, data)])`. At most 10 PDFs of at most 20 MB, each starting with `%PDF-`, and `brief.document_ids` must equal the uploads' content-derived `document_id`s; otherwise `SubmissionRejected` (422) before a Run exists. File bytes never enter graph state or a model call: they reach INGESTING through the compiled graph only, so they are not checkpointed. A Run resumed without them fails with that reason rather than silently dropping its documents.
- **INGESTING** (`pipeline/ingest.py`) runs only when documents are an allowed source type. Each distinct file is chunked once by `DoclingChunker` (`agents/doc_reader.py`: Docling with OCR and table structure off, `HierarchicalChunker`, page from the first provenance item, section heading from the last heading; any ordinary exception from conversion, chunking or metadata extraction becomes `ChunkingError`, while `KeyboardInterrupt` and `SystemExit` still propagate). Chunks are stored through `SourceStore.ingest_document` (Source ID from the content hash; each chunk a Passage with page and section heading) and indexed in `doc_chunks`. An unreadable PDF is logged (`document_unreadable`) and skipped, and so is one that cannot be indexed, for example with Qdrant down (`document_unindexed`); if none can be read, `documents` is dropped from the allowed types (`documents_unavailable`), and a Run with no other type fails.
- **`doc_chunks`** (`stores/doc_chunks.py`): one point per (owner, Run, chunk) with payload `{run_id, owner_id, source_filename, page, section_heading, source_id, observation_id, passage_index, text, embedding_model}`; `source_filename` is the name this Run uploaded the file under (the Source keeps the title of its first upload, which citations show). Every search is filtered by `owner_id`, `run_id` and `embedding_model` inside the class. The model name is recorded on every point instead of in collection metadata, which the pinned Qdrant server (v1.12) does not support; a collection with another vector size is refused. `SourceStore` stays the one door to chunk text: the payload copy is written once at ingestion and never read back by the pipeline.
- **Doc Reader**: its own subagent (`doc_reader`), with only the `doc_search` tool (k = 8). The tool returns `{chunk_id, source_id, source_filename, page, relevance_score}`, never text, and records a `doc_search` event with the searching Sub-task's ID, read from the Doc Reader's own task description (which the dispatch guard keeps canonical), never from a model-chosen argument. The dispatch guard routes each Sub-task to the subagent its `source_type` needs. A reply names chunk IDs (`obs_<id>:<index>`); the graph keeps only those that Sub-task's own searches returned for this Run's documents, and records them on the `SourceRef` as `passages`. A chunk returned only to another Sub-task is rejected.
- **Scoped reading**: Extraction and Synthesis read only the retrieved chunks of a document, by their own Observation and index (`SubTaskResult.passage_scope`, merged by `merge_scopes`); a web Source (no `passages`) is still read whole from its latest Observation. The same file uploaded to two Runs is one Source with an Observation per Run, and each Run indexes, reads and cites its own. The per-call cap and the Evidence budget are unchanged.
- **Citations and fact-checking**: unchanged code paths. A document Claim cites its chunk with page and section heading, and the Fact-Checker compares it with the cited document's stored chunks (`document_passages`); the verification policy (0.85, numeric check) is untouched by T7 (F2 revised it later; see the F2 notes).
- **Embeddings**: `FastEmbedEmbedder` (`BAAI/bge-small-en-v1.5`, 384 dimensions) is wired in production for Doc Reader search, Evidence relevance and Fact-Checker span retrieval (one model, section 10). Model files download once on first use, or up front with `insightforge-agent models download`.
- **Credibility**: an uploaded PDF scores 0.7 (`UPLOADED_DOCUMENT_CREDIBILITY`), a user-supplied primary document; document type and date are not inferred yet.
- **Concurrency**: parallel Web and Document Researchers share one repository connection. The repositories object owns one re-entrant lock (`SqliteRepos.lock`, `MemoryRepos.lock`); both Researcher tools and the dispatch guard's rejection log take it only around repository calls, never around search, fetching, embedding or model calls.
- **Retention**: a later Run cannot search earlier uploads (visibility), while a Report's citations keep resolving. Since T13 the retention sweep removes the chunks (Passages and `doc_chunks` points) of an expired Run's uploads that nothing retained refers to (see the T13 notes).
- **Tests**: scripted end to end with in-memory Qdrant (`tests/pipeline/test_t7_documents.py`); real Docling, fastembed and Qdrant offline from the local model cache (`tests/integration`, `-m integration`, excluded by default and in CI).

### T6 notes: Writer, Fact-Checker and citations

- **Writer** (`pipeline/write.py`): besides unknown evidence IDs and figures not in the cited item, a Claim is rejected when it names a capitalised non-initial word that neither the item's statement nor its entity contains. Offending Claims are listed in the retry prompt; after `writer_retries` the Run is FAILED with no Report.
- **Sampling** (`pipeline/fact_check.py`): `ceil(20% of Claims)`, at least 5, all Claims when fewer than 5; `random.Random(run_id)` picks positions, so a Run always checks the same Claims. Settings: `fact_check_rate`, `fact_check_minimum`, `similarity_threshold`.
- **Verdicts** (`ClaimVerdict`): each cited web Source is re-fetched once per fact-check and stored as a new Observation under the same Source (the original Observation, Passages and citation are untouched). Similarity uses the `Embedder` (since T7, production wires the fastembed model; tests that need exact vectors inject the hashing embedder). Since F2, `verified` needs a sentence span with similarity >= threshold that states every figure of the Claim and that one entailment call judges SUPPORTED (see the F2 notes). A failed re-fetch is `unchecked` and left out of `pass_rate`. Uploaded-document Claims are compared with the cited Observation's stored chunks (`document_passages`). Historical Claims are checked through their original Sources since T8 (see the T8 notes). `unverified` Claims get ` [UNVERIFIED]` appended in `VerifiedReport.draft`.
- **Summary** is stored in `body["verified"]["summary"]`: claims, sampled, verified, unverified, unchecked, `pass_rate` (None when nothing could be checked), threshold. Verdicts are in `body["verified"]["verdicts"]`.
- **Citations** (`pipeline/citations.report_citations`, `SourceStore.citation`): every Claim resolves to Source title, publication date, credibility score and the primary Passage text, from stored records and independent of the sample.
- **Known limit (resolved by F2):** whole-paragraph similarity rejected true Claims and passed wrong-measure Claims. F2 replaced it with span matching and entailment (see the F2 notes and ledger row 13).

### T8 notes: Memory researcher and Run summaries

- **Run summaries** (`domain/memory.py`, `RunSummaryRepository` in SQLite and in memory, table `run_summaries`): written once per Run by finalisation (first write wins), from stored records and with no model call: the Brief, the Report's section headings as themes, key entities (Evidence and Historical item entities, then Extraction entities), Gaps & limitations, owner, Watchlist Item and timestamp, each bounded (Brief 500 characters, at most 6 themes, 12 entities, 6 gaps of 200 characters). `memory_indexed` records whether the Report's Claims went into `report_sections`.
- **Planner context** (`pipeline/memory.summaries_for_planner`): the newest 3 summaries of the same User's COMPLETE Runs for the same Watchlist Item, or of their one-shot Runs for a one-shot Run. They are appended to the PLAN message only, never an earlier Report, Planner message or transcript; `run_summaries_loaded` logs the count and Run IDs only. `run_brief(..., watchlist_item_id=...)` records the item on the Run; Watchlist scheduling is still T11.
- **Eligibility** (`domain/memory.memory_eligible`, applied by `pipeline/memory.eligible_report_ids`): a Report is memory only if it is the User's own, its Run is COMPLETE (never a Failed Run), its summary exists with `memory_indexed` (at least one Claim indexed), and, in a Watchlist Run, its Run is not of the same Watchlist Item. `run_brief` computes the set once; it decides availability at submission and is fixed in graph state (`memory_reports`) as the only Reports the Memory researcher may search, so the two cannot disagree. Memory is runnable only when the `report_sections` index is wired.
- **`report_sections`** (`stores/report_sections.py`, setup shared with `doc_chunks` in `stores/vector_collections.py`): one point per Claim, payload `{owner_id, report_id, run_id, section_index, claim_index, heading, report_date, embedding_model}`, no text. Point IDs are derived from owner, Report and position, so indexing is idempotent. Every search is filtered by `owner_id`, the embedding model and an explicit Report ID set (an empty set searches nothing). It is a retrieval index only: Claims and lineage are always read back from the persisted Report. A Claim the Fact-Checker marked `unverified` is neither indexed nor usable as memory.
- **Memory researcher** (`agents/memory_researcher.py`, subagent `memory_researcher`, routed by `AGENT_FOR_SOURCE_TYPE`): `memory_search` (k = 5) returns `{ref, report_date, heading, relevance_score}` with `ref` = `<report_id>:s<section>:c<claim>`, never text, and logs a `memory_search` event naming the searching Sub-task (from its canonical task description, as in T7). The graph keeps only refs that Sub-task's own searches returned and that resolve to a Claim of an eligible Report; the rest are logged as `memory_refs_rejected` (invented, another Sub-task's, another User's, a Failed Run's, the same Watchlist Item's). `task` dispatch, the dispatch guard and repair are unchanged.
- **Historical/current separation**: a resolved ref becomes a `HistoricalItem` in `HistoricalContext` (graph state `historical`, Report body `historical`), never an `EvidenceItem`. It holds the Sub-task and Aspect, the earlier Claim, the originating Report ID, Run ID and Report date, the original As-of period (kept apart from the Report date), and the original primary and supporting Passages. A memory Sub-task has no Source refs, so Extraction and Synthesis never read earlier material; Synthesis may end with no Evidence when background exists, so a memory-only Run can complete. **Lineage is flat**: a Claim that was itself historical passes on its own original Report, date and Passages, never a chain.
- **Writer**: background is shown apart from the bundle as `- [hist_id] (heading; reported YYYY-MM-DD; as of period) statement`. Sections: one per bundle section for current Claims, and each Historical Claim in the section whose heading is its item's heading, compared ignoring case and spacing (the bundle section of that name, else a historical-only section). Every heading of the Historical context needs at least one well-placed Historical Claim, though not every item must be used. A Historical Claim must set `historical`, cite a Historical item and carry a recognised annotation of the original Report's date, `(reported YYYY-MM-DD)` or a leading `As reported on YYYY-MM-DD, ` (`strip_report_date`); a date anywhere else is substance, not an annotation. Its numbers and names are checked against the earlier statement once only that annotation is removed (`without_report_date`, the same helper the Fact-Checker uses). Marking an Evidence item historical, citing a Historical item as a current Claim, leaving a historical heading without a Claim or placing one under another heading are violations, all checked in `violations()` and retried as usual. Current Claims are checked exactly as before.
- **Fact-checking** (`pipeline/fact_check._historical_verdict`): a sampled Historical Claim, without its recognised date annotation (and whole when it has none), follows Historical item -> original primary Passage -> Observation -> Source, and goes through the unchanged F2 path: re-fetched if web (`historical_web_refetch`; a failed re-fetch is `unchecked`), or the original stored chunks if an uploaded PDF (`historical_document_passages`). The verdict's `original` is the inherited Passage; the citation drawer resolves the same Passage. The 0.85 gate, numeric guard, top 3 spans, entailment call and fail-closed rules are untouched.
- **Finalisation** (`pipeline/memory.finalise_report`): `complete` indexes the new Report's Claims and stores the Run summary before storing the Report and marking the Run COMPLETE, so a stored Report is always finalised. Both steps are idempotent, and the Report ID is derived from the Run (`derive_report_id`), so a crash between finalising and storing leaves a summary for a Run that is not COMPLETE (neither memory nor Planner context), and the resumed node finalises and stores that same Report. A stored Report without a summary (from before T8) is finalised before COMPLETE, both on graph resume and in `settle_interrupted` (the recovery `run_brief` uses); if that still fails, the Run is FAILED and never memory. An index outage at completion fails the Run with no Report.
- **Limitations**: Reports stored before T8, or finalised without the index wired, are never backfilled into `report_sections`. Historical items are bounded by k = 5 per search but are not counted in the 6,000-token Evidence budget. "Memory Sources inherit credibility" is realised through the original Sources the check follows; there is no separate memory Source row. Watchlist rules (web always on, no uploads) are enforced at creation in T11, and Historical Claims are kept out of Findings in T12, which can rely on `Claim.historical` and the separate `HistoricalItem` type. The full evaluation command is T16; `samples/briefs/memory-prior-report.yml` is the memory-path brief.
- **Tests**: `tests/domain/test_memory_rules.py`, `tests/stores/test_run_summaries_and_report_sections.py`, `tests/pipeline/test_t8_memory.py` (end to end, scripted models, hashing embedder, in-memory Qdrant) and `tests/pipeline/test_t8_historical_checks.py` (Writer rules and inherited fact-checks).

### T9 notes: Clarification and the startup sweep

- **The question** (`agents/planner.ask_clarification`): in PLAN the Planner either calls `submit_plan` or, when it judges the Brief vague (fewer than 3 Sub-tasks with a concrete scope and time horizon), `ask_clarification(question)`. The decision is the Planner's; there is no keyword, length or second-model check. The graph only enforces the rules: the question is honoured on the first PLAN reply of a one-shot Run with a timeout scheduler wired (`Deps.timeouts`). A second question, a question in a Watchlist Run, or one in a correction reply is refused (`clarification_refused`) and treated as "no plan", so T4's one corrective re-prompt, trimming and failure apply unchanged. A plan and a question in the same reply: the plan wins.
- **Pause** (`pipeline/graph`): `planning` returns the question and routes to the `clarify` node, whose only statement is `interrupt({"question": ...})`, so re-running it on resume has no side effect. An interrupt returns from `stream` normally (checked against LangGraph 1.2.14), so `drive` never treats it as a failure: it reads `get_state(config).interrupts` and calls `await_clarification`, which stores the question with `asked_at` and `deadline = asked_at + clarification_timeout` (default 30 minutes, `CLARIFICATION_TIMEOUT_MINUTES`) and enters `AWAITING_CLARIFICATION` in one compare-and-set (`RunRepository.open_clarification`: only from PLANNING, only if the Run never asked), then schedules the timeout. The Planner and outer-graph threads are derived from the Run (`run_thread_id`), so a restarted process finds both checkpoints.
- **Durable state** (`domain/models.Clarification`, column `runs.clarification`, added by a migration for older databases): question, asked_at, deadline, resolution (`answered | timed_out`), answer, resolved_at. It lives on the Run row, so the state and the resolution change in one write and cannot disagree.
- **Timeout job** (`scheduler/clarification.py`): a one-shot APScheduler 3 `date` job at the deadline, id `clarification-timeout:<owner>:<run>`, in the SQLAlchemy job store in the application database (`apscheduler_jobs`), stored by reference (`fire_timeout(owner_id, run_id)`). The application installs the handler (`ClarificationService.time_out`) at startup. APScheduler 3 writes jobs only once started (paused is enough). Verified: a due job survives a shutdown (`tests/test_t9_scheduler.py`).
- **Resolution** (`pipeline/clarification.ClarificationService`): the answer (`POST /research/{run_id}/clarify`, `api/clarify.py`) and the timeout share `RunRepository.resolve_clarification`, one conditional `UPDATE ... WHERE state='AWAITING_CLARIFICATION'` that sets RESUMING and the resolution together. The first resolver wins; the loser gets `ClarificationConflict` (409 for the endpoint, a no-op for the job). An answer is accepted only before the deadline and a timeout only at or after it; the deadline never changes, so these checks need no lock, and the compare-and-set alone decides a race. A successful answer then cancels the job (clean-up only); a job that fires before the deadline schedules itself again. Another User's Run is 404; an empty or overlong answer is 422. `GET /research/{run_id}/clarification` returns the question, deadline and resolution.
- **Resume** (`pipeline/graph.resume_run`): `claim_resume` (RESUMING -> PLANNING, a compare-and-set) lets exactly one caller continue; it resumes the outer graph with `Command(resume=<stored resolution>)`, and `planning` sends `PLAN (clarified)` with the answer or `PLAN (clarification timed out)` on the same Planner thread. On a timeout the Planner states its assumption in `submit_plan(assumption=...)`; the graph records `The clarifying question "..." was not answered before its deadline, so the plan rests on a best-effort reading of the Brief: <assumption>` as the first entry of Gaps & limitations (and `plan_assumption` in the Run log). The plan bounds apply as before, so a timeout can end in FAILED (no Report, no summary, never memory).
- **Startup sweep** (`pipeline/recovery.startup_sweep`, run once at startup): the design's "left RUNNING" means any in-progress stage (`INGESTING`, `PLANNING`, `RESEARCHING`, `EXTRACTING`, `SYNTHESIZING`, `WRITING`, `FACT_CHECKING`); there is no `RUNNING` state. Those go through `settle_interrupted` (FAILED, or COMPLETE if the Report was already stored, as in T8). RESUMING is resumed with the stored resolution; an expired AWAITING_CLARIFICATION is timed out through the same compare-and-set; an unexpired one is left waiting and its job scheduled again (replacing any survivor); COMPLETE and FAILED are never read. A crash after the resolution is stored is resumed, including one right after the claim, while the outer checkpoint still waits at the question (`awaits_resume`: nothing of the resumed Run has run). Once the graph has taken the answer in, a crash is an ordinary in-progress Run and FAILED, so no step runs twice. The sweep must run once per process, before the executor accepts work.
- **Limitations**: T10 mounts the router, supplies the persona dependency and passes its `RunExecutor` as the service's `submit` (required, so a request or the scheduler thread never runs the pipeline itself); until then nothing starts the scheduler or calls the sweep outside tests. A crash between the interrupt checkpoint and storing the question leaves the Run in PLANNING, which the sweep fails. The clarification itself is not shown in the Report beyond the timeout limitation.
- **Tests**: `tests/stores/test_clarification_repository.py` (compare-and-set on both backends, real two-connection SQLite races, migration), `tests/pipeline/test_t9_clarification.py` (end to end on SQLite files with restarts, a fake clock and scripted models; the endpoint through FastAPI's TestClient) and `tests/test_t9_scheduler.py` (real APScheduler and job store).

### T10 notes: API, RunExecutor, SSE and personas

- **Composition root** (`api/app.py`): `Runtime.build` opens SQLite (`DATABASE_URL`), the LangGraph checkpointer (`CHECKPOINT_DATABASE_URL`), Qdrant and the APScheduler job store, and wires `Deps`, the `BoundedRunExecutor`, `ClarificationService(submit=executor.submit)` and `ResearchService`; `create_app` mounts the routers and runs `start`/`stop` in the lifespan, off the event loop (a failed `build` closes whatever it had opened). **Startup:** scheduler started paused (jobs writable, none firing), `install_timeout_handler(ClarificationService.time_out)`, `startup_sweep` once, scheduler resumed, then requests are served. **Shutdown:** scheduler paused and shut down, handler removed, executor shut down (queued work settled, running Runs waited for), then Qdrant, the checkpointer and SQLite closed.
- **RunExecutor** (`pipeline/executor.py`): `RUN_CONCURRENCY` worker threads (default 2) plus `RUN_QUEUE_LIMIT` queued new Runs (default 2). A new Run reserves capacity *before* it is created (`admit`), so a full or closing executor is 503 with `Retry-After` and no Run exists. A resume of an already resolved clarification (`submit`) is never refused for capacity; it waits for a worker. Work queued at shutdown is cancelled: a new Run is settled FAILED ("the process shut down before the Run started"); a resume logs `resume_deferred` and stays RESUMING, which the next startup sweep resumes. A Run created just as shutdown begins is settled FAILED, never left in its first state.
- **Submission** (`pipeline/graph.py`): `run_brief` is `check_brief` (the Submission check, no writes; 422 with no Run) → `start_run` (the Run stored in its first state) → `carry_run` (the graph). `POST /research/run` does the same through `ResearchService` and returns 202 with `run_id` once the Run is stored and queued. Source toggles default to the persona's; documents are base64 in the JSON body.
- **SSE** (`GET /research/status?run_id=`, `api/research.py`): tails `run_events` with `read_after` in batches of 100, `id:` is the persisted `seq`, `Last-Event-ID: N` streams exactly `seq > N` (absent or empty: from the start; anything but up to 18 ASCII digits: 400). `enter()` now stores a stage's event before its state, so every event of a Run is stored before it is terminal; the stream, once it sees COMPLETE or FAILED, drains the rest, sends `event: end` and closes. A disconnect stops only the stream. Ownership is checked before the cursor (another User's Run is 404 whatever the header). While the Run is unfinished a stream lives at most `SSE_MAX_STREAM_SECONDS` (default 60) and then closes without `end`; EventSource reconnects with `Last-Event-ID`. **Shutdown under uvicorn** (measured, `tests/api/test_t10_uvicorn_shutdown.py`): uvicorn waits for open connections to close before the lifespan shutdown runs, so under plain `uvicorn --factory` an open stream holds shutdown for up to that lifetime (60.25 s observed once with the default; the test uses a 4 s lifetime). `insightforge-agent serve` (`api/app.serve`) runs a `uvicorn.Server` whose exit handler first calls `Runtime.begin_shutdown`, so streams end on the signal (0.4 s observed; the test asserts under 10 s with the 60 s default) and the ordered shutdown follows.
- **Personas** (`api/session.py`): `GET /personas`, `POST /session` sets cookie `insightforge_persona` = persona ID + HMAC-SHA256 under `SESSION_SECRET` (stdlib `hmac`), HttpOnly, SameSite=Lax. `PersonaSession.current_user()` is the one dependency; a missing, malformed, tampered or unknown-persona cookie is 401. Persona selection, not security (README).
- **Ownership:** every endpoint resolves through owner-scoped repositories; an unknown and a foreign Run or Report are the same 404. `/reports` lists and retrieves only; there is no delete.
- **Concurrency fixes:** concurrent Runs share one SQLite connection, so every repository method holds the shared lock. Web Observations: `derive_observation_id` includes the Run, and Extraction and synthesis read `SourceStore.read_run_source` (the Run's own latest Observation of a Source), never the User's latest, which another Run may have fetched. Citations are unchanged: they name their Observation and Passage.
- **Limitations:** a hard kill leaves running Runs in progress for the startup sweep to fail (as before). No multipart upload; no export or share links (T15); the citation drawer endpoint arrived with the T14 backend prerequisite; Watchlist endpoints are T11. Issue #49's observations remain T20.
- **Tests:** `tests/api/` (app-level, scripted models, real SQLite files, real APScheduler, real restarts; `LiveStream` drives the ASGI app directly to read and drop a live SSE stream) and `tests/pipeline/test_t10_observation_provenance.py`.

### T11 notes: Watchlist CRUD, ticks and first-success persistence

- **HTTP:** `POST /watchlist`, `GET /watchlist`, `GET /watchlist/{id}`, `PATCH /watchlist/{id}`,
  `DELETE /watchlist/{id}`. Bodies use `brief`, positive integer `cadence_minutes`, optional
  positive integer `max_runs`, and `source_toggles`. Web must be enabled; documents and upload
  fields are rejected. Memory is optional. Unknown configuration fields are rejected. Every
  lookup is owner-scoped (foreign and missing IDs both return 404).
- **Creation validation:** a Planner-only call uses the existing Planner prompt, planning tools,
  and plan bounds; at most one corrective reply. A clarification question rejects the Vague
  Brief immediately with 422, before an item, Run, or checkpoint exists. The call has no research
  tools. Updates that change the Brief or source choices repeat this validation. These are model
  calls in production; all T11 tests supply scripted models.
- **Editing:** cadence, source choices and `max_runs` can change. PATCH requires the revision from
  the client's last read; the repository compares it atomically and returns 409 for a stale edit.
  A cadence change resets the next tick to one new cadence from the edit; other changes preserve
  a future due time, and a tick that fell due within the last cadence stays due, so the edit's
  new revision runs it at once instead of losing it to the revision check. An item more than one
  cadence overdue, such as one re-enabled long after its limit was reached, restarts one cadence
  from the edit. The
  due-time merge and revision check use the current row in one repository transaction, so a PATCH
  cannot overwrite a tick's newer due time. Every successful edit increments the revision. After
  any Run started, changing the standing Brief returns 409: create a new item so its established
  state and research scope stay aligned.
  Deletion stops scheduling, removes the item's Baseline input, and retains Runs and Reports.
  A Run admitted before deletion is allowed to finish; it cannot recreate the deleted item.
- **Admission:** `ResearchService`/`RunExecutor.admit` reserve capacity before `start_run`. The
  Watchlist repository stores the Run in a `BEGIN IMMEDIATE` transaction only if the item belongs
  to that owner, its revision is current, no Run is non-terminal, and `max_runs` allows it. Run
  rows themselves are the durable count. The tick's revision is carried through Brief checking
  into that transaction, so an intervening edit cannot start the old Brief or source choices.
  Shutdown admission is checked in the same transaction. Failed and queued-then-cancelled Runs
  consume a slot; refusal before persistence consumes none. `AWAITING_CLARIFICATION` and `RESUMING` block overlap.
  `run_brief(watchlist_item_id=...)` now also validates ownership and source restrictions (#49),
  and `check_brief` binds every Watchlist Run to its item: the Brief text must equal the
  standing Brief and the enabled sources must equal the item's (a false toggle and an omitted
  one are the same), with no documents or uploads; otherwise `SubmissionRejected`.
- **Ticks:** persisted APScheduler interval jobs share T9's scheduler/job store. Jobs carry owner,
  item and revision, with `max_instances=1` and coalescing. The durable `next_tick_at` comparison
  is the only late/missed decision: a callback up to one second early and dispatch delay through
  one cadence are accepted, while a callback more than one cadence late advances beyond the
  missed intervals without starting a Run.
  SQLite consumes each due time once; duplicate, stale, missed and overlapping callbacks do not
  start another Run, and each skip is logged. Capacity refusal, and a Brief check that finds no
  usable source, skips that tick with no immediate retry. Once the Run limit is reached the
  interval job is removed. Raising the limit through PATCH restores scheduling. `sync` re-reads
  the current row under the repository lock that PATCH holds around its update and sync, so a
  tick's cleanup holding an older snapshot cannot remove a schedule a concurrent limit raise
  restored.
- **Recovery/order:** install both handlers while paused, run T9 recovery, repair Watchlist
  completion inputs and persisted schedules, advance due times beyond downtime, then resume.
  Deleted-item jobs are removed. One lock-free event closes both SSE and Watchlist admission;
  the admission transaction rechecks it, then shutdown follows T10's
  scheduler → executor → resources order. Waiting on active worker threads is unchanged.
- **Baseline boundary with T12:** T11 persists the first successful Run's initialization input
  exactly once in `watchlist_baselines`: current Claims, their structured Evidence, Passage refs,
  Source IDs, Observation dates, As-of periods, and establishing Run/Report provenance. This is
  stored state, not a pointer to a Report. Historical and unverified Claims are excluded; every
  supporting Observation must belong to the establishing Run. `normalization_pending=true`
  explicitly identifies these as **seeds, not normalized Findings**. T12 owns normalized Finding
  identity, polarity, controlled predicate cardinality, temporal establishment, Diff updates and
  Alerts. It must normalize these persisted seeds before any comparison; T11 does not claim that
  normalized per-Finding state already exists. Empty current input remains distinguishable from
  no successful Run. Later successes and all failures leave the seed state unchanged.
- **Completion:** the scheduler's post-processing callback runs after COMPLETE, outside the
  research graph, including resumed/recovered Runs. Initialization is attempted from this
  callback and startup recovery, never from a tick. A temporary failure schedules a persisted
  one-shot retry job one minute later (`watchlist-baseline:<owner>:<item>`), which reschedules
  itself while it keeps failing; it does not depend on the research interval, so it also works
  after the last allowed Run. It queries COMPLETE Runs through the Watchlist index rather than
  scanning all owner Reports. Only `InvalidBaselineCandidate` quarantines that Report for the
  current Baseline seed schema, so the next successful Report can establish the Baseline.
  Conversion raises it only for explicit defects of the candidate's own stored data: a body
  that is not a mapping or fails Pydantic validation, a Claim citing missing Evidence, a
  missing Observation, Source or Passage, an Observation from another Run, or a non-web
  Source. Any other error, including an unexpected `AttributeError` or `TypeError`, a store
  error or `MemoryError`, is retried, so it can never reject the earliest success.
  Incrementing the schema version makes old candidates eligible for re-evaluation after a
  compatibility fix. No initialization error can prevent later research or process startup. No
  Diff or Alert is created by T11.
- **Dashboard:** `item.max_runs`, `runs_started`, `completed_runs`, `failed_runs`, and the five
  most recent Failed Run records, read by a bounded query on the Watchlist Run index. No Watchlist status is added. Failure details remain available
  through the existing owner-scoped Run events/SSE endpoint.
- **Tests:** `tests/api/test_t11_watchlist.py`, `tests/api/test_t11_lifecycle.py`, and
  `tests/stores/test_watchlist_repository.py` cover CRUD, owner/source validation, races,
  capacity, cancellation, persisted schedules, clarification, recovery and initialization.
- **T20 follow-up:** SQLite item writes and APScheduler's separate job-store transaction cannot
  commit atomically. Startup repairs missing/stale jobs; an in-process job-store write failure
  currently surfaces to the caller and may require restart to reconcile. Review this with T9-1
  in #49. Existing T9/T10 hardening entries remain unchanged.

### T12 notes: Findings, Diff, Alerts and webhook

- **Findings** (`domain/findings.py`): the controlled vocabulary (`DEFAULT_REGISTRY`) declares
  each predicate's cardinality, object type (`quantity`, `entity`, `text`), aliases, negated
  aliases (`discontinued` is `offers` negated) and tolerance. Every declared tolerance is zero:
  quantities compare as exact `Decimal` values after `canonical_value`, other objects after
  `normalise`. Identity is `(subject, predicate, scope)` or, multi-valued, plus the canonical
  object; the As-of period never takes part. Each Finding stores its provenance: Run, Report,
  Claim, Evidence item, primary and supporting Passages, Source IDs and Observation dates.
- **Eligibility:** a later Run's Claims go through the same `report_seeds` conversion as the T11
  seed: current Claims only (never Historical or `unverified`), every supporting Observation
  fetched by that Run from a web Source.
- **Finding judge** (`agents/finding_judge.py`, the existing `light` model): called in batches
  of 20 only for Claims whose predicate is outside the vocabulary, whose triple is incomplete,
  or whose predicate the vocabulary marks `polarity_judged` (membership predicates such as
  `offers`, which prose can deny). No tools, no Passage text, a strict JSON schema. Its answer
  must name a vocabulary predicate (or `unsupported`), and its subject and object must be stated
  by the Claim, the Evidence statement or its structured fields. Only an explicit `unsupported`
  answer skips a Claim (logged with the Diff). A polarity-only request carries its
  `fixed_triple`; the answer must repeat that predicate, subject and object (case and spacing
  aside) and only its polarity is used. An invalid reply, a missing judgment or an answer
  that fails those checks is a failed interpretation: `NormalizationFailed` fails the attempt and
  the Diff retry lifecycle applies (a failed seed normalisation stays `seed` and is retried), so
  an eligible Claim never silently counts as Not reconfirmed. A quantity that is not one exact
  value ("about $100") is skipped, never compared as text.
- **Seed and established states:** `baseline_state` is `none`, `seed` (T11 input) or
  `established`. The seed is normalised once, before any Diff, into `watchlist_findings`; the
  T11 seed row is never modified. UNKNOWN-period, conflicting and older values never establish.
- **Comparison** (`domain/diff.py`, pure): against the latest established state of each Finding,
  never a preceding Report. Outcomes: `added`, `changed`, `no_longer_true` (material);
  `reconfirmed` (resets the count, refreshes provenance and period); `not_reconfirmed` (value
  kept; unconfirmed at K, setting `NOT_RECONFIRMED_THRESHOLD`, default 3, without an Alert);
  `older_ignored` (by As-of period end; two derived periods on one day by observation time);
  `ambiguous` (UNKNOWN period, a different value or the opposite polarity for the same stated
  period, or conflicting values in one Run). A negated Finding with no established counterpart
  is recorded as `added` (the negative knowledge is established) but is not material and raises
  no Alert: nothing established was true before, so no real-world change is implied. Issue #13's
  "a new Finding" is read as a new affirmed Finding; a denial is material only as
  `no_longer_true` of an established affirmed value, and affirming what was denied is `added`
  (material). An ambiguous or older restatement neither confirms nor refutes, so it also counts
  as Not reconfirmed.
- **Execution** (`scheduler/diff.py`): the completion callback and startup recovery schedule a
  persisted APScheduler job `watchlist-diff:<owner>:<item>`, outside the graph. It processes the
  item's COMPLETE Runs after the initializing Run in `(created_at, id)` order, and waits while an
  earlier Run is still active. One-shot Runs are never queued. Run status is never written.
- **Durability** (`stores/diffs.py`): `watchlist_diffs` has one row per Run; each attempt is
  counted before it starts, so a crash loop ends after 2 attempts. A failure is retried once,
  no earlier than its stored `retry_after` (one minute later), and later Runs wait; another
  completion callback, a restart or a second connection cannot bring it forward, because
  `begin_attempt` checks `retry_after` in its transaction. A started attempt holds a 10-minute
  lease (`attempt_started_at`) checked the same way, so concurrent callers count nothing and
  wait for it; after a crash the next attempt starts when the lease ends; a second failure is permanent, leaves the Baseline
  unchanged, and the next successful Diff lists it (`intervening_failed`) and shows the net
  change. The commit (Finding updates, Baseline `version`, Diff record, Alert, delivery row) is
  one `BEGIN IMMEDIATE` transaction that checks the Diff is still pending, the version is
  unchanged and the Run is the earliest unfinished one. Alerts are `UNIQUE(owner_id, run_id)`.
  The per-item lock in the service only avoids duplicate work in one process.
- **Alerts:** one per Run with a material change, holding the new Report, and per change the
  Report and Run that established the earlier value, plus the intervening failed Diffs. A
  derived period reads "Change detected on [date]"; a stated one "As of [period]". Read state is
  stored. Alert and Diff HTTP endpoints and the UI belong to T15.
- **Webhook** (`scheduler/webhook.py`): only when the owner's persona has `webhook_url`. Slack
  incoming-webhook JSON (`text` fallback plus `blocks`). Delivery state is its own table, created
  with the Alert. Each POST is counted before sending; failures back off 30 s, 2, 8 and 32 min,
  and the fifth failure is permanent. Startup reschedules pending deliveries. Delivery is at
  least once. No webhook error reaches the Diff, the Alert or the Run.
- **Deletion** removes the established state with the seed; the Diff log and Alerts are kept.
- **Limitations:** the judge's grounding check is lexical, so text that itself names an entity
  can still steer a judged Finding; judged Findings can vary between Runs with model
  nondeterminism; subject aliases ("BYD" / "BYD Auto") are not resolved; multi-process
  deployment is out of scope (the constraints hold across connections, see the tests). A Report
  with a stored-data defect (`InvalidBaselineCandidate`, e.g. a foreign Observation) fails its
  Diff as a whole, after the one retry, rather than skipping the bad Claim; the next Diff shows
  the net change. Seed normalisation has no attempt cap: like the T11 Baseline retry, a seed whose
  interpretation keeps failing is retried every minute and no Diff runs meanwhile. A
  `DiffConflict` (only possible with concurrent writers)
  consumes an attempt. Ordering relies on T11's one-active-Run admission: Runs of an item
  complete in creation order, so the initializing Run is the earliest non-quarantined success.
- **Tests:** `tests/domain/test_t12_findings.py`, `tests/domain/test_t12_diff.py`,
  `tests/stores/test_t12_diff_lifecycle.py`, `tests/stores/test_t12_webhook.py`,
  `tests/api/test_t12_diff_app.py`.

### T13 notes: Retention sweep

- **Cutoff** (`domain/retention.py`): `cutoff = now - RETENTION_DAYS` (default 30, a positive
  integer), on the injectable UTC clock. A terminal Run is expired when its terminal timestamp
  is at or before the cutoff: a COMPLETE Run's Report `created_at`, a FAILED Run's failure
  record `retention_starts_at`. A non-terminal Run, a COMPLETE Run without a Report and a FAILED
  Run without a record are never candidates, however old.
- **Protected references** (`ProtectedRefs`, per User): every persisted Report (Evidence item
  `primary`, `supporting`, `contradicting`; Historical item `primary`, `supporting`; verdict
  `original`, `matched`; a FAILED Run's stored Report too), every Baseline seed, established
  Finding, Baseline-normalisation detail and Diff log entry (both its Finding and the previous
  one), and Alerts. Each record is validated against its model and then walked whole, so a
  reference in a field the collector does not name is still protected. A record that is not
  JSON or fails validation raises `MalformedRetentionRoot`, and the sweep removes nothing of
  that User; so does a Passage `index` that is not a non-negative integer. A protected Passage
  keeps its Observation and that Observation's Source; an Observation named only by
  `observation_dates` keeps its row; a reference to an Observation with no Passage index keeps
  every Passage of it.
- **What goes** (`stores/retention.py`), only for an expired Run: unprotected Passages of its
  Observations (a web Observation Passage by Passage; an uploaded document only whole, because a
  document Claim is fact-checked against every stored chunk of its Observation); an Observation
  and its split marker once no Passage is left and nothing names it; its Sub-task rows; its
  links to Sources it no longer holds an Observation of (a link to retained material stays, so
  `SourceRepository.list_for_run` still lists the Sources behind an expired Run's citations);
  its progress events except `stage`, `run_failed` and `completion_recovered`; a Source with no
  Observation, no Run link and no reference. `sources.run_id` decides nothing.
  Runs, Reports, Run summaries, failure records, extracted facts, Baseline seeds, established
  Findings, the Diff log, Alerts and `report_sections` points are never removed.
- **Extracted facts**: `ExtractedFact.provenance` is `available` (Source and Passage set) or
  `unavailable` (both None), enforced by a validator. When a Passage is removed, its facts are
  rewritten as unavailable in the same transaction; their substantive fields are unchanged.
  Rows stored before T13 read as `available`. Extraction and conflict detection are unchanged.
- **Failure records** (`FailureRecord`, table `run_failures`): owner, Run, Run creation time,
  failure time, `retention_starts_at`, stage (the state the Run failed in), reason (at most
  1,000 characters) and error codes. `RunRepository.fail` writes the record and FAILED in one
  `BEGIN IMMEDIATE` transaction; the first record is kept. Every path to FAILED goes through
  `settle_interrupted` with a code: `pipeline_failure` (RunFailed), `unexpected_error`,
  `process_interrupted` (startup sweep), `not_started` (shutdown cancellation),
  `checkpoint_missing`, `clarification_conflict`, plus `report_not_finalised` when a stored
  Report could not be finalised. A FAILED Run from before T13 is backfilled by the next sweep:
  `backfilled`, no failure time or stage, the logged `run_failed` reason when there is one,
  code `legacy_unrecorded`, and retention starting at backfill time (never `Run.created_at`).
- **Batches and atomicity**: per User, about 100 Passages per batch (an uploaded document is
  never split, so its batch may be larger). The User's retained records are read under the
  shared lock once per sweep and interpreted outside it, to plan; `apply` re-reads protection
  and eligibility and deletes in one `BEGIN IMMEDIATE` transaction, so a Report, Diff commit or
  Source link written meanwhile is seen. The lock is not held between batches.
- **Qdrant and SQLite** (`DocChunkIndex.delete`): exact point IDs (owner, Run, Observation,
  index), filtered by owner and Run, idempotent. Inside `apply`, after the re-check and before
  the SQLite delete, so a chunk that became protected is never removed from Qdrant; this holds
  the lock for one Qdrant call per batch that has document chunks, bounded by
  `DELETE_TIMEOUT_SECONDS` (10 s, applied to the HTTP request and as the server-side timeout). If Qdrant fails,
  those Observations are kept and skipped for the rest of the sweep. If the process fails after
  Qdrant and before SQLite commits, SQLite is unchanged and the next sweep removes the same
  chunks again; a missing point is not an error. Without a `doc_chunks` index wired, document
  chunks are kept.
- **Scheduling** (`scheduler/retention.py`): one persisted APScheduler interval job,
  `retention-sweep`, daily, first run 10 minutes after it is first stored, coalesced, one
  instance. Startup installs the handler while paused and adds the job only if the job store
  lacks it, so restarts never postpone it, and a missed run fires once after startup. Shutdown
  sets the closing event (the sweep stops between batches) and removes the handler. A
  re-entrant call returns at once. A sweep with nothing newly expired changes nothing.
- **Limitations**: there is no endpoint or UI for failure records yet (T15); the Watchlist
  dashboard still lists Failed Runs from Run rows. LangGraph checkpoints are not compacted, so
  a Run's checkpoint may still hold IDs of removed Passages (never their text). Passages and
  split markers left by a crash before their Observation row was stored carry no Run and are
  not swept. `apply` re-reads all of a User's retained records per batch, so a sweep's cost
  grows with the User's Reports and Watchlist history. The in-memory repositories have failure
  records but no sweep.
- **Tests**: `tests/stores/test_t13_retention.py` (each protection rule, boundaries, Qdrant
  failures, crash between stores, idempotency, restart, owner isolation, malformed records,
  writes racing a batch), `tests/stores/test_t13_watchlist_regression.py` (Baseline seeding,
  Diffs and Alerts after sweeps), `tests/pipeline/test_t13_retention_pipeline.py` (failure
  records on every path; citations, memory and inherited fact-checks after a sweep of real
  Runs) and `tests/test_t13_scheduler.py` (job store, restart, app registration).

### T14 notes: Next.js workbench

- **Layout** (`web/`): Next.js 16 App Router, React 19, TypeScript, plain CSS. Pages are Server Component shells (`app/runs/[runId]`, `app/reports/[reportId]`); the workflow is Client Components with `useReducer`, `fetch` and native `EventSource`; no state library, no route handlers.
- **Same origin:** `next.config.ts` rewrites `/api/:path*` to `INSIGHTFORGE_API_ORIGIN` (default `http://127.0.0.1:8000`). Measured through `next dev` and `next start`: SSE frames arrive one by one through the rewrite, and the HttpOnly cookie set by `/api/session` rides on every later `/api/*` call.
- **Typing:** `lib/api-schema.d.ts` is generated from the OpenAPI schema (`npm run api:types`); `lib/api.ts` is the only fetch wrapper and never retries (`POST /research/run` is not idempotent). The untyped Report body is narrowed only in `lib/report.ts`, which reads `body.verified.draft` (never `body.draft`, so `[UNVERIFIED]` is kept) and maps citations by section/Claim position and `evidence_id`.
- **Persona boundary:** everything on a page belongs to one User. A successful switch from one persona to another is a full load of `/` (`lib/navigation.hardNavigate`), which drops the Brief, attached PDFs, the Run's EventSource and any Report. Choosing a first persona (none selected) keeps the start page, so a Brief drafted before choosing survives; on a Run or Report page it reloads that same page, which then loads as the chosen persona.
- **Attached PDFs:** while any PDF is attached, *Your documents* is held on and its checkbox locked (the backend reads uploads only when `documents` is enabled); the checkbox, the estimate and the submission use the same effective toggles, and removing the last PDF restores the User's choice (a toggle reports only the source that changed, so the forced value is never saved as the choice).
- **Progress:** `lib/run-reducer.ts` keeps connection state (`CONNECTING | CONNECTED | RECONNECTING | LOST | CLOSED`) apart from Run state. An `EventSource` error while it retries is RECONNECTING, never FAILED; only `event: end` ends a Run. Replayed events (`seq <= lastSeq`) and unknown types are ignored; RESUMING and AWAITING_CLARIFICATION are never rail steps; INGESTING appears only when the Run ingested uploads. A reload rebuilds progress from the replayed stream (no stored cursor).
- **Clarification:** `GET /research/{id}/clarification` is the authority. The countdown is presentation; at zero the card re-reads the clarification. A 409 re-reads and shows the winning resolution; the answer is never resent. A timeout is shown as research continuing, and after the Run ends the card keeps a record of how the question was resolved.
- **Report:** a numbered marker per Claim opens a native `<dialog>` drawer with the Citation's Source title, publication date, credibility score, page, section heading and Passage text from `GET /reports/{id}/citations`. A citation failure (500 for broken provenance) leaves the Report readable with an explicit notice and disabled markers.
- **Tests:** `web/tests/` (Vitest + Testing Library: reducer, parsers, API client, components) and `web/e2e/` (Playwright against `next start` and `tests/web_e2e/server.py`, the real app with scripted models; its test-only `/__test__` routes hold fetches, move the clock and fire the clarification timeout). The scripted Planner asks its question whenever the Vague Brief appears in its prompt, including earlier Run summaries, so e2e clarification tests use a separate persona.
- **Limitations:** no live estimate refinement (above); Watchlist, Diff, Alerts, export and share links are T15.

### T16 notes: Sample brief library, eval command and evaluation report

- **Library** (`samples/briefs/*.yml`, loaded by `evaluation/library.py`): 5 briefs, each a different industry and question type: company deep dive from an uploaded annual report (T7), follow-up on an earlier Report through memory (T8, with a `setup` Run for the same User), competitive landscape across named companies, regulatory impact on named companies, and market sizing with an outlook. The three new briefs are web only. Each declares its expected Report structure (headings framed by Executive Summary and Gaps & limitations, `source_types`, `must_cite`, notes) and checks. A malformed brief, a missing document, or a toggle without its documents or setup Run is refused, naming the file.
- **Command** (`evaluation/cli.py`): `insightforge-agent eval --provider P [--provider Q] --judge-provider J|none [--brief ID] [--library] [--out]`. It makes real model and search calls and is never run by the test suite. Every provider's and the judge's Settings are validated, and the judge and every provider's Deps built, before any Run starts: a usage or configuration error exits 2, and a judge or pipeline that cannot be built exits 1, either way with no evaluation output left behind. Every Run goes through `run_brief` with `build_deps` (no timeout scheduler, so clarification is off and a question is refused). Each provider has its own SQLite files under `<out>/<timestamp>/<provider>/` and an in-process Qdrant; the app's database is never used. Each sample runs as its own User (`eval-<id>`), so memory and Run summaries never cross samples. Writes `eval-report.json` and `eval-report.md`.
- **Fact-check pass rate** (`evaluation/metrics.py`): read from the persisted `body["verified"]` and cross-checked against its verdicts and the stored draft: the summary's Claim count is the draft's, and each verdict names a distinct Claim of the draft by position and cites that Claim's item. Any disagreement is a metric error, never a score, and is never repaired. `verified / (verified + unverified)`, `unchecked` excluded, None when nothing was checkable. Totals pool Claims across Reports.
- **Citation accuracy**: every Claim, resolved per Claim through `pipeline.citations.claim_citations` (the path `report_citations` uses, now exposed per Claim; `report_citations` still raises on the first gap). Accurate when the Claim resolves and its primary Passage states every figure of the Claim (`unsupported_numbers`); a Historical Claim's report-date annotation is not a figure. Unresolved Claims stay in the denominator. Counted by kind (web, document, historical, and unknown for a current Claim that does not resolve). It measures provenance and figures, not semantic support.
- **Synthesis quality** (`evaluation/judge.py`, rubric `synthesis-rubric-v1`): one call per completed Report to the judge provider's Writer model (`build_chat_model`; the generation settings set on that model, such as Anthropic's `max_tokens`, are recorded, and anything unset is the provider's default); coverage, organisation, synthesis, calibration, whole numbers 1 to 5, mean as the score. The judge sees the Brief, the expected structure and the published draft, never Passage text, and is told not to judge accuracy or citations. An invalid, incomplete or failed reply is `error`; no judge is `not_judged`; neither is ever a score.
- **Per provider** (`evaluation/report.py`): configured model names (`llm.configured_model_name`) and the classes and model ids that actually ran, with the embedder, search and fetcher, and the pipeline conditions read from the Deps its Runs used. A Run that did not complete (`failed`, `rejected`, `setup_failed`, `error`) is listed with its reason and left out of every total. With several providers, comparability is decided per metric: never when their conditions differ, otherwise only when measured on the same briefs (one judge scores all providers by construction).
- **Section 11 rule**: decided on the Anthropic run of the full library. There, completeness is decided first: a rate that cannot be computed, or a brief that was not measured, is `undetermined` (its reason keeps the rate and whether it is below 80%). Only with every brief measured is a pooled pass rate under 80% `required` (a documented remediation attempt), else `not_required`. A partial (`--brief`) run is always `undetermined`, with its rate and whether it is below 80% in the reason. Other providers are `not_applicable`. The command flags it; the remediation itself belongs to T18.
- **Tests**: `tests/evaluation/` (scripted tier): metrics over Reports stored through the real SourceStore, the judge over scripted replies, and the command end to end over the real pipeline with scripted models (`tests/evaluation/world.py`: web, document and two-step memory briefs, failed, partly failed and setup-failed Runs, drifting re-fetches), each checked against the written files and the persisted Runs. The scripted research content is shared EV fixture material for every web brief, so these tests show orchestration: each sample's own Brief, source types and expected structure reach the Planner and the judge. Research quality on each brief's real subject is T18's to judge.
- **Limitations**: the briefs' expected structure and `checks` are descriptive (the judge reads the structure; no metric enforces the checks); one Run per brief per provider (no repeats or variance); Run records do not store their models, so identity is recorded by the eval at run time; no live run has been made in T16 (T18 runs Anthropic).
