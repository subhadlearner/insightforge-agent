# InsightForge technical design

Status: complete, pending the user's final confirmation. Vocabulary is defined in `../CONTEXT.md`; decisions that are hard to reverse are in `adr/`. Source spec: `../assignment_04_market_intelligence.md`.

## 1. Scope and users

- Build the research pipeline first, then scheduling and diffing, then the API and UI on top of them.
- Users are 3 to 4 personas declared in `users.yml` (see section 9). Users are strictly isolated from each other.
- Single process, single node. Postgres, a worker queue and real accounts are deliberate non-goals.

## 2. Pipeline

LangGraph is the outer graph and the Planner is one node in it (ADR-0001).

```
INGESTING -> PLANNING -> RESEARCHING -> EXTRACTING -> SYNTHESIZING -> WRITING -> FACT_CHECKING -> COMPLETE
                |                                                       ^  |
                v                                                       +--+ (retry, max 2)
        AWAITING_CLARIFICATION                           any stage -> FAILED
```

- `INGESTING` is skipped when there are no uploaded documents (section 5).
- `AWAITING_CLARIFICATION` is a non-terminal state entered when the Planner asks its one question. It uses LangGraph interrupt and resume with the checkpoint in the database. `POST /research/{run_id}/clarify` answers it. After a configurable timeout (default 30 minutes) the Run proceeds on a best-effort reading and records the assumption in Gaps & limitations. The crash sweep (section 11) ignores this state.
- `RESEARCHING` runs a supervisor deep agent that calls `task` once per Sub-task. A post-condition checks at most 6 Sub-tasks and a result for every Sub-task ID, and marks any missing one `Failed` (ADR-0003).
- `EXTRACTING` is its own stage so Researchers stay isolated (ADR-0002).
- `FAILED` is terminal and produces no Report. It happens when every Sub-task fails, the Writer exhausts its retries, or a Run-level limit trips.
- A failed Sub-task does not fail the Run. It is listed in Gaps & limitations.

## 3. Stage contracts

One pydantic model per transition, defined in `domain/`. A stage reads its input model plus the stores, and nothing else.

| Transition | Model | Contents |
|---|---|---|
| submit | `Brief` | text, source toggles, document IDs |
| plan | `SubTaskPlan` | flat list of Sub-tasks: `id, query, source_type, time_horizon_months, priority` |
| research | `ResearchResults` | per Sub-task: `source_refs[]`, one-line summary, status |
| extract | `ExtractionResult` | entities, relationships, conflicts |
| synthesize | `EvidenceBundle` | sections of Evidence items, each with Confidence |
| write | `ReportDraft` | sections of Claims, each with an `evidence_id` |
| fact-check | `VerifiedReport` | the draft plus Claim verdicts and the fact-check summary |

Graph state carries IDs and small structured values only, never Source bodies. A test asserts that serialised state holds no body above a size limit.

## 4. Context engineering

- **Scratch store.** Sources and Observations live in our database behind a `SourceStore` interface. `read_source(source_id)` is a thin tool over it. PDF chunk text lives in Qdrant, and the same interface resolves those IDs. Source IDs are content-derived and immutable within a Run. They are kept for as long as their Report exists.
- **Isolation.** Researcher subagents are defined with their own `tools=` and receive only their Sub-task. Never use fork mode.
- **Budget.** The Evidence bundle respects a token budget (default 6,000). Overflow is compressed, never truncated, and each cut is logged with its reason.
- **Memory.** At start the Planner reads the last 3 Run summaries for the same Watchlist Item, or for the same User on one-shot Runs. It never reads transcripts.
- **Compaction.** DeepAgents compacts history automatically. There is no `summarization=True` flag, and the Run log records each compaction so the README's context-blowout evidence can be produced.
- **Planner todos.** `write_todos` is opt-in, so `TodoListMiddleware` is passed explicitly.

## 5. Agents

- **Planner.** Takes the Brief and the User's source toggles and produces the `SubTaskPlan`. A Vague Brief gets exactly one clarifying question. A Watchlist Item's Brief is validated for vagueness when it is created.
- **Ingestion.** Docling extracts and chunks the PDFs (at most 10, 20 MB each) into the `doc_chunks` collection, with `{run_id, owner_id, source_filename, page, section_heading}`.
- **Web Search.** The provider is chosen by setting. Fetches the full body of the top 5 results, scores credibility, skips paywalled pages and logs them.
- **Doc Reader.** Semantic search over `doc_chunks`, k = 8, returning chunk IDs and never text.
- **Memory researcher.** Searches `report_sections` for prior Reports.
- **Extractor.** Entities and relationships as JSON, persisted to the `entities` collection, conflicts marked `conflict: true`.
- **Synthesis.** The only agent allowed to pull full raw content. Dedupes, assigns Confidence per Evidence item, ranks lexicographically (recency bucket, then credibility, then Sub-task priority) and compresses to budget.
- **Writer.** Produces the Report with the required sections. Every Claim cites an existing Evidence item and every number and named entity must appear in it. Violations trigger up to 2 retries with the offending Claims listed as a constraint.
- **Fact-Checker.** Samples 20% of Claims (minimum 5), seeded from the Run ID. Verdicts are `verified | unverified | unchecked` (section 7).
- **Diff Agent.** Compares Findings as triples against the Baseline (section 8).

## 6. Persistence

- **Database (SQLite behind repository interfaces).** Users' Runs, Sub-tasks, Sources, Observations, Reports, Watchlist Items, Alerts, `run_events`, revoked share links, and the APScheduler job store.
- **Qdrant.** `doc_chunks` (Run-scoped), `entities` (cross-run), `report_sections` (prior Reports). One configurable embedding model, with its name recorded in each collection's metadata. Every query is filtered by `owner_id` inside the repository.
- **Progress.** The pipeline appends to `run_events`. The SSE endpoint tails it with a cursor (`Last-Event-ID`), so a reconnecting client loses nothing.

## 7. Credibility, Confidence and fact-checking

- **Credibility score** (0 to 1) exists on every Source: web by domain type, recency decay and byline; uploaded documents by document type and date; memory Sources inherit from the Sources behind them.
- **Confidence** belongs to an Evidence item: `HIGH` needs 2 or more Independent Sources, `MEDIUM` is a single high-credibility Source, and `LOW` is a single low-credibility Source or conflicting signals. A section's label is the lowest among its items.
- **Fact-check.** Re-fetch the Source, chunk the page into paragraphs, embed into a temporary Run-scoped collection, and take the maximum similarity between the Claim and any chunk. A Claim passes at similarity of at least 0.85 (configurable). A numeric Claim also needs its number in the matched chunk. The best-matching chunk is stored with the verdict. `unchecked` (the re-fetch failed) is excluded from the pass rate.
- **Non-web Sources.** For uploaded documents, run the same max-over-chunks check against the stored chunks of the cited document, which is the original text and not the Evidence item. For `memory` Sources, follow the inheritance chain to the original Sources and check those, re-fetching if they were web pages. The verdict records which kind of check ran.

## 8. Scheduling and diffing

- APScheduler runs one Run per Watchlist Item per tick, using the same pipeline. At most one active Run per item. Overlapping and missed ticks are skipped.
- Baseline is the most recent completed Report for the item. The first Run stores a Baseline and raises no Alert.
- A Finding is a normalised triple (subject, predicate, object). A Material change is a new triple, a missing one, or an object that differs beyond tolerance. An LLM judge handles only Findings that do not reduce to a triple.
- The Diff Agent runs in the scheduler, outside the pipeline graph, after a Run reaches `COMPLETE`. A Diff failure is logged, retried once, and never changes the Run's status. The Baseline still advances to the new Report. Manual one-shot Runs are not diffed.
- One Alert per Run with at least one Material change. Optionally POSTed to the webhook with retry and backoff. Webhook failure never fails the Run.

## 9. API and identity

- `POST /research/run`, `GET /research/status` (SSE), `/watchlist` CRUD, `/reports` (list, retrieve, export), plus Alerts.
- Identity: a persona picker in the UI sets a session cookie holding the persona ID, signed with the app secret and marked HttpOnly. A FastAPI dependency resolves it to a `User`. This is persona selection and not security, and the README says so. One User never sees another's data, and a request for it returns 404. Personas in `users.yml` carry `id`, `display_name` and optional defaults (source toggles, webhook URL), with no keys.
- Share links are signed JWTs holding the Report ID and expiry, revocable through a table. They expose the Report, the fact-check summary and web Source metadata, and hide uploaded-document excerpts, the Run log and every other Report.
- Export: Markdown, PDF, and the share link. PDF is rendered with Playwright's headless Chromium from the same HTML the viewer uses, behind an interface so another renderer can replace it. The README notes the one-time `playwright install chromium`.

## 10. Configuration and limits

One typed settings object (pydantic-settings, from the environment and `.env`). The app refuses to start when a required secret for the chosen provider is missing. `.env.example` is committed and `.env` is ignored.

- Model name, provider keys, search provider (`tavily | brave | serpapi`), Qdrant URL, database URL, personas file path.
- Evidence token budget 6,000, max Sub-tasks 6, Fact-Checker rate 20% (minimum 5) and similarity threshold 0.85, share-link TTL 7 days, JWT secret.
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
  scheduler/   APScheduler and Diff
```

Dependencies point inward, so `domain` imports nothing from the other packages. An import-linter check enforces it. Runs execute in the API process behind a `RunExecutor` interface, with a concurrency cap and a startup sweep that marks Runs left `RUNNING` by a crash as `FAILED`.

An `insightforge-agent eval` command runs the 5 sample briefs and writes the evaluation report. Fact-check pass rate and citation accuracy are deterministic, and synthesis quality uses an LLM judge with a fixed rubric.

## 12. Facts to verify at build time

These are library facts the design relies on and that have not been confirmed against the installed packages. Each is checked in the slice that first needs it.

- A `deepagents` graph, or a custom backend, can be invoked from inside a LangGraph node, and the supervisor reliably emits parallel `task` calls (ADR-0003).
- `deepagents` supports the Python version in `.python-version` (the project requires 3.12 or newer).
- LangGraph interrupt and resume works with the chosen checkpointer in the same database (section 2).
- Docling's chunking yields page numbers and section headings for the `doc_chunks` metadata.
- Qdrant can create and delete a temporary collection per fact-check cheaply enough, or a payload-scoped alternative is used instead (section 7).
- Token counting for the Evidence budget matches the provider's tokenizer closely enough for a deterministic test.
