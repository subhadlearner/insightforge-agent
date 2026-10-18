# InsightForge

An autonomous research platform: a user submits a research question (a **Brief**) and receives
a cited intelligence **Report**. Standing Briefs can be re-researched on a schedule, and
material changes are surfaced as **Alerts**.

> **The persona picker is identity selection for the demo. It is not authentication and must
> not be treated as a security boundary.** Anyone who can reach the API can select any persona
> in `users.yml`. The session cookie is signed (HMAC with `SESSION_SECRET`) and HttpOnly only so
> that a browser cannot switch persona by editing it; there are no passwords and no sign-up.

Vocabulary is defined in [`CONTEXT.md`](CONTEXT.md), the design in
[`docs/design.md`](docs/design.md), and hard-to-reverse decisions in [`docs/adr/`](docs/adr/).
This README uses those terms exactly (Source, Observation, Passage, Evidence item, Claim,
Finding, Historical Claim, Baseline, Alert, Run summary).

**How claims in this README are labelled.** *Implemented* means the code does it.
*CI-tested* means an offline test in the default suite asserts it. *Locally exercised* means
it was run by hand and the record is linked. *Design-specified* means `docs/design.md` requires
it. *Not yet verified* means nobody has shown it on a real Run. The fresh-clone record behind
the setup and test sections is [`docs/t17-verification.md`](docs/t17-verification.md).

## Contents

1. [What InsightForge does](#what-insightforge-does)
2. [Architecture](#architecture)
3. [Getting started from a fresh clone](#getting-started-from-a-fresh-clone)
4. [Running it](#running-it)
5. [Testing](#testing)
6. [Context engineering and evidence](#context-engineering-and-evidence)
7. [Evaluation](#evaluation)
8. [Security, privacy and limitations](#security-privacy-and-limitations)
9. [Anthropic verification results (T18): pending](#anthropic-verification-results-t18-pending)

## What InsightForge does

A **Run** carries one Brief through a bounded multi-agent pipeline:

```
INGESTING -> PLANNING -> RESEARCHING -> EXTRACTING -> SYNTHESIZING -> WRITING -> FACT_CHECKING -> COMPLETE
                |  ^                                                    ^  |
                v  |                                                    +--+ (Writer retry, max 2)
  AWAITING_CLARIFICATION -> RESUMING                     any stage -> FAILED
```

1. **INGESTING** (only when PDFs were uploaded): Docling chunks each PDF; every chunk is a
   Passage, indexed in Qdrant.
2. **PLANNING**: the Planner splits the Brief into Aspects and a flat plan of 3 to 6
   **Sub-tasks**, each with one source type (`web`, `documents` or `memory`). A Vague Brief
   gets one clarifying question (one-shot Runs only).
3. **RESEARCHING**: one isolated Researcher per Sub-task. Web Researchers fetch **Sources**.
   Each fetch is stored as an **Observation**, which is split once into addressable
   **Passages**. The Doc Reader searches the uploaded PDFs. The Memory researcher finds Claims in
   the User's earlier Reports.
4. **EXTRACTING**: structured facts per Source, plus conflicts between those facts.
5. **SYNTHESIZING**: de-duplicated **Evidence items**. Each one is validated against its
   Passages and given a Confidence, then ranked and cut to a token budget.
6. **WRITING**: the Writer drafts the Report. Every **Claim** cites an Evidence item, or a
   Historical item (the earlier Claim a **Historical Claim** rests on, with its original
   Passages).
7. **FACT_CHECKING**: a seeded 20% sample of Claims (at least 5) is re-checked against
   re-fetched or stored Passages. Each sampled Claim is `verified`, `unverified` (marked
   `[UNVERIFIED]`) or `unchecked`.

A **Failed Run** produces no Report. A single failed Sub-task does not fail the Run: if
enough research succeeds, the Report lists the failure under Gaps & limitations.

Also implemented: web research and uploaded-PDF research (T7), memory of earlier Reports as
Historical Claims (T8), clarification with a durable timeout (T9), the HTTP API and live
progress stream (T10), Watchlist Items on a schedule (T11), Findings, Baselines, Diffs, Alerts
and an optional Slack-format webhook (T12), a retention sweep (T13), the Next.js workbench
(T14), Markdown/PDF export and revocable Share links (T15), and the evaluation harness
(T16). Status: implemented and covered by the offline suite with scripted models; only the
happy path, planning, extraction and synthesis, and Fact-Checker cases have been run on a real
model (see the last section).

## Architecture

```
Browser ── Next.js (web/) ──/api/* rewrite──> FastAPI (api/) ── RunExecutor (bounded threads)
                                                  │                       │
                                       APScheduler (scheduler/)     LangGraph outer graph (pipeline/)
                                       ticks, Diffs, webhook,        │  PLANNING + RESEARCHING:
                                       clarification timeouts,      │  one checkpointed DeepAgents
                                       retention sweep              │  Planner thread (agents/)
                                                  │                  │  -> isolated Researcher subagents
                                                  └──────────┬───────┘
                                                             v
                             stores/: SQLite (records, run_events, checkpoints, job store)
                                      Qdrant (doc_chunks, report_sections) + local FastEmbed
```

| Package (`src/insightforge_agent/`) | Responsibility |
|---|---|
| `domain/` | Pydantic stage contracts and pure rules: plan bounds, periods, quantities, Confidence, Findings, Diff. No I/O, and it imports no other package |
| `pipeline/` | The LangGraph outer graph (`graph.py`), with one module per stage: `ingest`, `research`, `extract`, `synthesize`, `write`, `fact_check`. Also citations, memory, recovery and the bounded `RunExecutor` |
| `agents/` | Prompts and DeepAgents definitions: the Planner (`create_deep_agent` root), the web Researcher, the Doc Reader, the Memory researcher and the Finding judge |
| `stores/` | `SourceStore` (the only way to reach Passage text), SQLite repositories, and the Qdrant collections `doc_chunks` and `report_sections` |
| `api/` | FastAPI routers, persona session, SSE progress, export and share delivery |
| `scheduler/` | APScheduler jobs: Watchlist ticks, Diff, webhook delivery, clarification timeout, retention |
| `evaluation/` | The `eval` command, metrics, the synthesis judge and the report writer (T16) |
| `benchmark/` | The offline Evidence-quality benchmark tools (`insightforge-agent bench`) |

How the pieces connect:

- **LangGraph and DeepAgents** ([ADR-0001](docs/adr/0001-langgraph-outer-graph-planner-as-node.md),
  [ADR-0003](docs/adr/0003-research-fan-out-via-task-verified-by-graph.md)). LangGraph owns the
  stage sequence, checkpoints, interrupt and resume. The Planner is a DeepAgents root agent
  that spans PLANNING and RESEARCHING on one checkpointed thread. It dispatches one `task` per
  approved Sub-task. The graph then checks that every Sub-task ID got a result and runs one
  repair round for missing ones. A dispatch guard refuses any `task` call that is not an
  approved Sub-task.
- **Extraction is its own stage** ([ADR-0002](docs/adr/0002-extraction-is-its-own-stage.md)),
  so Researchers stay isolated and conflicts are found over structured facts, not raw text.
- **SQLite** holds Runs, Sources, Observations, Passages, Reports, the `run_events` Run log,
  Watchlist state, Findings, Diffs and Alerts. It also holds the LangGraph checkpoints
  (`CHECKPOINT_DATABASE_URL`) and the APScheduler job store.
- **Qdrant** holds two vector indexes: uploaded-PDF chunks (`doc_chunks`) and earlier Reports'
  Claims (`report_sections`). Embeddings come from a local **FastEmbed** model
  (`BAAI/bge-small-en-v1.5`), so they do not depend on the chat provider. Every query is
  filtered by owner inside the repository.
- **Docling** parses and chunks uploaded PDFs, offline, with OCR off.
- **Chat models** come from one factory (`llm.build_chat_model`) with three roles: planner,
  writer and light. `LLM_PROVIDER` selects `gemini`, `groq` or `anthropic`.
- **Import boundaries** are enforced by import-linter (`[tool.importlinter]` in
  `pyproject.toml`): `domain` imports nothing else, production code never imports `benchmark`
  or `evaluation`, and the benchmark never reaches model construction or the full graph. This
  keeps the rules testable without models and keeps measuring tools out of the product.

## Getting started from a fresh clone

### Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Git | any | cloning |
| [uv](https://docs.astral.sh/uv/) | a recent release (verified with 0.12.5) | Python environment; installs Python 3.12 from `.python-version` if missing |
| Node.js and npm | Node 20.9 or later (CI uses 24) | the web UI and browser tests |
| Docker | any recent | optional: a persistent Qdrant server (`docker compose`) |
| Chromium for Playwright | installed by `npx playwright install chromium` | PDF export and browser tests |

Verified on Windows 11 with uv 0.12.5, Python 3.12.14, Node 24.20.0 and npm 11.19.0. CI runs
on `ubuntu-latest`. See [`docs/t17-verification.md`](docs/t17-verification.md).

### Install

```bash
git clone https://github.com/subhadlearner/insightforge-agent.git
cd insightforge-agent
uv sync --locked                                  # Python dependencies (downloads packages)
uv run insightforge-agent models download         # optional: FastEmbed and Docling model files
cd web && npm install && cd ..                    # web UI dependencies
cd web && npx playwright install chromium && cd ..   # optional: PDF export and browser tests
```

`models download` fetches model files from Hugging Face once into the local cache. It needs
network access but no API key. Without it, the files download the first time a PDF is ingested
or something is embedded, and the integration tests skip.

### Configuration

```bash
cp .env.example .env
```

`.env` is git-ignored. Never commit it, and never put keys in `users.yml`. What you need
depends on what you run:

| Mode | Needs | Network | Cost |
|---|---|---|---|
| Offline: tests, import contracts, `demo`, browser E2E | nothing in `.env` | package and browser downloads only. Tests fail on any outbound connection | none |
| API and UI, no Runs | `SESSION_SECRET` (32+ characters), the key for `LLM_PROVIDER`, and `TAVILY_API_KEY`. Startup checks that they are set, without calling either service. **A Tavily key is required even if you never submit a Run:** SerpAPI and Brave are not implemented, so a SerpAPI or Brave key alone stops the API at startup with `SearchError: no implemented search provider is usable` | none at startup | none |
| Real Runs and Watchlists | a real key for `LLM_PROVIDER` (`gemini` by default, `groq`, or `anthropic`) and a real `TAVILY_API_KEY` (Tavily is the only implemented search provider) | provider, search and fetched web pages | provider and search usage; Gemini/Groq free tiers are rate-limited |
| `eval` and `pytest -m live` | the above, plus the explicit opt-ins described below | as above | real, possibly paid calls |

The app refuses to start without `SESSION_SECRET`, without the key for the chosen
`LLM_PROVIDER`, when no listed search provider has credentials, or when Tavily is not among
the usable ones (the others are listed in `SEARCH_PROVIDERS` but not implemented). `.env.example` documents
every setting.

## Running it

### API (T10)

Start Qdrant and the API:

```bash
docker compose up -d qdrant                       # Qdrant v1.12.4 on localhost:6335
uv run insightforge-agent serve --port 8000
```

`QDRANT_URL` defaults to `http://localhost:6335`. `QDRANT_URL=:memory:` runs Qdrant inside the
process instead, so no Docker is needed, but nothing it indexed survives a restart.

Stop it with Ctrl+C: open progress streams end at once (clients reconnect with
`Last-Event-ID`), new Runs are refused from that moment (503), the scheduler and executor stop,
running Runs finish, and the databases are closed. Queued new Runs are settled FAILED and
queued clarification resumes stay RESUMING for the next start. Shutdown waits for running Runs
with no time limit, so a stalled Run keeps the process from exiting; after a hard kill, startup
handles each unfinished Run by its stored state (resuming or rescheduling clarifications,
settling the rest). Plain `uvicorn insightforge_agent.api.app:app_from_env --factory` serves
the same app, but on shutdown uvicorn then waits for open streams for up to
`SSE_MAX_STREAM_SECONDS` (60 s).

Runs execute in the API process on a bounded pool (`RUN_CONCURRENCY` workers, `RUN_QUEUE_LIMIT`
queued new Runs); a submission beyond that is refused with 503. Endpoints:

| Endpoint | What it does |
|---|---|
| `GET /personas`, `POST /session {"persona_id"}`, `GET` / `DELETE /session` | list and select a persona (sets the signed HttpOnly cookie) |
| `POST /research/run {"brief", "source_toggles"?, "documents"?}` | 202 with `run_id`; 422 if no source type is available; 503 at capacity |
| `POST /research/estimate {"source_toggles"?, "document_count"?}` | a heuristic runtime estimate in seconds, shown before submit; creates nothing |
| `GET /research/status?run_id=` | SSE progress (the Run log); reconnect with `Last-Event-ID` to resume |
| `GET /research/{run_id}/clarification`, `POST /research/{run_id}/clarify {"answer"}` | the Run's clarifying question; 409 once resolved |
| `GET /reports`, `GET /reports/{report_id}` | the selected persona's Reports (there is no delete) |
| `GET /reports/{report_id}/citations` | one citation per Claim, in draft order: the Source, its date and credibility score, and the exact Passage the Claim cites |

Another persona's Run or Report is 404. Documents are sent as `{"filename", "content_base64"}`.
Omitted `source_toggles` mean the persona's defaults from `users.yml`, else web only, for both
`/research/run` and `/research/estimate`. Citations are resolved from the Report's persisted
provenance (a Historical Claim shows the Passage it inherited, never a fact-check re-fetch); a
Report whose provenance does not resolve is 500, never a partial list. The estimate is
`base + document_count x pdf_ingest + one cost per enabled source type`, with coefficients from
`RUNTIME_ESTIMATE_*`: a rough guide, not a promise, and it checks nothing, so a submission can
still be refused with 422.

### Web UI (T14)

The Next.js app in `web/` is the research workbench: pick a persona, write a Brief, choose
sources and attach PDFs, see the runtime estimate, follow the Run live, answer a clarifying
question, and read the Report with a citation drawer for every Claim. Run the API as above,
then in a second terminal:

```bash
cd web
npm run dev
```

Open http://localhost:3000 (use `localhost`: `next dev` blocks its development assets for
other host names such as `127.0.0.1`). The browser only talks to Next; `/api/*` is rewritten
to FastAPI, so the persona cookie and the live progress stream are same-origin and no CORS is
configured. `INSIGHTFORGE_API_ORIGIN` (default `http://127.0.0.1:8000`) tells the Next server
where FastAPI is; `next dev` reads it at start, and `next build` bakes it into the build that
`next start` serves.

### Offline demo: Extraction and synthesis

Exercise the real Extraction and synthesis stages with seeded Sources and scripted models. No
API keys, no network, no extra dependencies:

```bash
uv run insightforge-agent demo
```

It prints the extracted facts and conflicts, the ranked Evidence with its supporting Sources,
Confidence labels and decision reasons, corroboration across number formats ("4.27 million" and
"4,270,000"), a contradiction between two Sources, and the candidates the pipeline rejects. It
ends with a PASS/FAIL list and exits non-zero if any expectation fails.

Only the model is scripted (`src/insightforge_agent/demo.py`); every decision shown is made by
the production pipeline. To see the whole app working without keys, run the browser E2E suite
(below). It drives the real API through the UI with scripted models and recorded search.

### Uploaded documents (T7)

PDF uploads (at most 10, 20 MB each) are chunked by Docling and searched semantically in Qdrant
with the local embedding model. No API key is needed for either. Uploads are visible only to
the Run they were sent with; a later Run needs them uploaded again. The sample document Brief
is `samples/briefs/document-annual-report.yml`, which uses
`samples/documents/northwind-health-annual-report-2025.pdf` (a fictional company).

### Watchlist API (T11)

With a selected persona, create a standing Brief with `POST /watchlist`:

```json
{
  "brief": "Compare US electric vehicle makers' pricing, sales and battery plans for 2026",
  "cadence_minutes": 15,
  "max_runs": 3,
  "source_toggles": {"web": true, "documents": false, "memory": false}
}
```

Creation checks that the Planner can produce a concrete plan (a model call in production).
A Vague Brief is rejected before storage. Web is required, uploads are prohibited, and memory
is optional. Omit `max_runs` for unbounded scheduling only at cadences of at least 15 minutes.
The first tick is one cadence after creation; downtime and overlapping ticks are skipped.

List with `GET /watchlist`, retrieve with `GET /watchlist/{id}`, edit with `PATCH` at that path,
and delete with `DELETE`. A PATCH includes the `revision` returned by the last read plus the
fields to change; a stale revision returns 409. Responses include `item`, `runs_started`, `completed_runs`,
`failed_runs`, `recent_failed_runs` (up to five), and the first-success Baseline seed state.
The UI can render `runs_started` of `item.max_runs` runs started. Executor capacity refusal
creates no Run and consumes no slot; a persisted Run that fails or is cancelled consumes one.

A cadence edit resets the next tick; other edits preserve a future due time, and a tick that
fell due within the last cadence still runs. An item more than one cadence overdue, including
one re-enabled long after reaching `max_runs`, restarts one cadence after the edit instead of
firing immediately from a stale schedule. No edit resets the Run count. Once any Run has
started, use a new item to change its Brief. Deleting an item retains its Runs and Reports and
lets already admitted work finish. All endpoints use the existing owner-scoped 404 behavior.

The first successful Run seeds the Baseline. Each later successful Run is diffed against it,
outside the pipeline graph (T12). A **Material change** (a new Finding, a changed value, or
one shown to be no longer true) raises one **Alert** per Run. A Finding that is merely absent
is Not reconfirmed and raises no Alert. If the persona has a `webhook_url`, the Alert is also
POSTed as Slack incoming-webhook JSON.

### Diffs, Alerts, export and Share links (T15)

| Route | Purpose |
|---|---|
| `GET /watchlist/{item_id}/diffs` | owner-scoped persisted Diff history |
| `GET /watchlist/{item_id}/diffs/{run_id}` | one persisted Diff |
| `GET /alerts`, `GET /alerts/{alert_id}`, `PATCH /alerts/{alert_id}` | owner-scoped durable Alerts and read state |
| `GET /reports/{report_id}/export.md` | owner-scoped verified Markdown download |
| `GET /reports/{report_id}/export.pdf` | owner-scoped Chromium PDF download |
| `GET /reports/{report_id}/shares` | list the current owner’s active, unexpired, non-revoked links for this Report |
| `POST /reports/{report_id}/shares` | create a signed Share link |
| `DELETE /reports/{report_id}/shares/{share_id}` | revoke one Share link |
| `GET /share/{token}` | token-scoped public Report projection; no persona required |

The Watchlist UI is at `/watchlist`; a Watchlist Item links to its persisted Diff history,
and `/alerts` shows durable material-change Alerts with links to the new and previous
establishing Reports.

Report actions provide Markdown and PDF downloads plus a revocable Share link. Markdown uses
the fact-checked Report and preserves `[UNVERIFIED]` markings. PDF does not maintain a second
Report template: the API launches Playwright headless Chromium through
`web/scripts/render-pdf.mjs`, signs the owner's persona cookie for that browser context, and
prints the existing `/reports/{id}` viewer HTML. It therefore needs `npx playwright install
chromium` and a running Next app at `WEB_ORIGIN` (default `http://localhost:3000`, to match the
required `next dev` hostname).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ValueError: SESSION_SECRET is required to sign the persona cookie` | Set `SESSION_SECRET` in `.env` (at least 32 characters) |
| `Search provider 'tavily' skipped: no credentials`, then a `Settings` validation error | No listed search provider has a key. Set `TAVILY_API_KEY` |
| `SearchError: no implemented search provider is usable` | Only a SerpAPI or Brave key is set, or `tavily` is missing from `SEARCH_PROVIDERS`. Set `TAVILY_API_KEY` and keep `tavily` in the list |
| `failed to connect to the docker API` | Docker is not running. Start it, or set `QDRANT_URL=:memory:` for a throwaway in-process Qdrant |
| The UI loads without styles or scripts | You opened `127.0.0.1:3000`. Use `http://localhost:3000` |
| PDF export fails | Run `npx playwright install chromium` in `web/`, and keep the Next app running at `WEB_ORIGIN` |
| `pytest -m integration` reports skips | Model files are not cached. Run `uv run insightforge-agent models download` once |
| Loopback HTTP tests fail behind a proxy | Unset `ALL_PROXY`/`HTTP(S)_PROXY` for the test process (see [`docs/t11-verification.md`](docs/t11-verification.md)) |

## Testing

| Suite | Command (from the repo root unless noted) | Needs | In CI |
|---|---|---|---|
| Default offline suite (tiers 1-3 and everything not `live` or `integration`) | `uv run pytest` | nothing; outbound network is blocked by `tests/conftest.py` | yes, split by tier |
| One tier | `uv run pytest -m baseline` / `replay` / `scripted` | nothing | yes |
| Integration (real Docling, FastEmbed, in-memory Qdrant) | `uv run pytest -m integration` | model files from `models download`; runs offline and **skips** without them | the step runs, but CI has no model cache, so these tests skip there |
| Live (real model or server) | `INSIGHTFORGE_ALLOW_LIVE_TESTS=1 uv run pytest -m live` | real keys; **makes paid calls** | never |
| Import contracts | `uv run lint-imports` | nothing | yes |
| Frontend unit and component tests (Vitest) | `npm test` in `web/` | `npm install` | yes |
| TypeScript | `npm run typecheck` in `web/` | `npm install` | yes |
| Production build | `npm run build` in `web/` | `npm install` | yes |
| Browser E2E (Playwright) | `npm run test:e2e` in `web/` | `npx playwright install chromium` and `uv`. Starts `tests/web_e2e/server.py` (the real app, scripted models, no keys) and `next start` itself | yes |
| Generated API types are current | `npm run api:types` in `web/` against a running API, then `git diff` | a running API | yes |

In PowerShell, set the variable first: `$env:INSIGHTFORGE_ALLOW_LIVE_TESTS = "1"; uv run pytest -m live`
(it stays set for that session; `Remove-Item Env:INSIGHTFORGE_ALLOW_LIVE_TESTS` clears it).
`pytest -m live` alone is not enough: live tests also need `INSIGHTFORGE_ALLOW_LIVE_TESTS=1`,
and skip without it.

Results on a fresh clone of `main` at `22dad4d` (2026-10-18, Windows 11). The uv, model and
Chromium caches were already warm, so cold downloads were not exercised. Details and caveats
are in [`docs/t17-verification.md`](docs/t17-verification.md):

| Check | Result |
|---|---|
| `uv run pytest` | 1,887 passed, 26 deselected (7 min 10 s) |
| `uv run pytest -m integration` | 19 passed, **only because the local model cache was already warm**; CI has no cache and skips all 19 |
| `uv run lint-imports` | 5 kept, 0 broken |
| `uv run insightforge-agent demo` | exit 0, every expectation PASS |
| `npm test` / `typecheck` / `build` | 102 passed / clean / built |
| `npm run test:e2e` | 14 passed |
| `docker compose up -d qdrant` | **not executed**: no Docker daemon on the verifying machine |

Test counts show coverage, not correctness. The default suite uses scripted models, so it
proves orchestration, persistence, provenance and the deterministic rules. It does not prove
how well a real model researches.

## Context engineering and evidence

A Run can touch dozens of web pages, each thousands of tokens long. InsightForge keeps any one
model call bounded by deciding, stage by stage, what text each model may see. Raw text lives in
stores and is passed around by ID, and only a few stages read Passage text, one bounded batch
at a time.

| Boundary | Why it limits context growth | Status and evidence |
|---|---|---|
| **Store by reference.** Sources, Observations and Passages live in SQLite (PDF chunks also in Qdrant) behind `SourceStore`. Graph state and checkpoints carry IDs and short structured values, never page bodies | A 20,000-character page costs the graph nothing. Only a stage that needs the text loads it | CI-tested: `tests/pipeline/test_happy_path.py::test_graph_state_carries_no_source_body` and `tests/pipeline/test_happy_path.py::test_no_source_body_in_any_checkpoint_or_intermediate_state` (a sentinel in a 23k-character page never appears in state or in either checkpoint) |
| **Researchers see summaries, not pages.** The fetch tool writes each Source's one-line summary in its own bounded call. The Researcher sees only `{source_id, title, summary}` | Five full articles per search would otherwise sit in the Researcher's context ([design rationale Q18](docs/design-rationale.md)) | CI-tested: `tests/pipeline/test_isolation.py::test_each_researcher_receives_only_its_own_subtask` |
| **Isolated Researchers.** Each Researcher is a DeepAgents subagent with its own `tools=` and only its Sub-task's task description. It does not see the Brief, the plan or its peers. Only its final message returns to the Planner | The Planner's thread grows by one short result per Sub-task, not by every search and fetch | CI-tested: same test, plus `tests/pipeline/test_isolation.py::test_the_researcher_subagent_has_its_own_tools_and_is_not_forked`. Isolation on a real model was checked in the T1 spike (`docs/design.md` section 12) |
| **Planner memory is three Run summaries.** The Planner gets the last 3 Run summaries for the same Watchlist Item, or for the same User's one-shot Runs. Each summary is bounded (Brief 500 characters, 6 themes, 12 entities, 6 gaps). It never sees transcripts, earlier Reports or Passage text | Memory stays constant in size however many Runs a User has made | CI-tested: `tests/pipeline/test_t8_memory.py::test_the_planner_reads_exactly_the_newest_three_summaries_and_no_transcript`. The Run log records `run_summaries_loaded` with count and IDs only |
| **Bounded Passage batches.** Extraction (one Source at a time), Synthesis (one Sub-task at a time) and the Fact-Checker read Passage text in batches under `PASSAGE_TOKEN_CAP` (default 3,000 estimated tokens, measured on the rendered batch). An oversized Passage is read in windows, never truncated | The cost of one call does not grow with page or document length | CI-tested: `tests/pipeline/test_extraction_synthesis.py::test_synthesis_payloads_respect_the_per_call_cap_separately_from_the_bundle_budget` (captures the payloads); `tests/pipeline/test_t5_review.py::test_an_oversized_passage_is_read_in_bounded_windows_by_extraction_and_synthesis` |
| **Evidence budget for the Writer.** The Writer gets a ranked Evidence bundle under `EVIDENCE_BUDGET_TOKENS` (default 6,000) plus the Historical context, and never sees Passage text. When the bundle is over budget, whole lowest-ranked items are cut (never truncated mid-item), and each cut is logged as an `evidence_cut` event with its reason, Confidence and rank | The Writer's input is bounded however much research succeeded | CI-tested: `tests/pipeline/test_extraction_synthesis.py::test_bundle_respects_the_budget_and_every_cut_is_logged`. Nothing is summarised: the implementation drops whole items (design §4 was corrected to say so). Historical items are bounded by search k = 5 but are not counted in the 6,000 budget (T8 limitation) |
| **Historical context stays apart.** Claims from earlier Reports enter as Historical items with their original Report, date and Passages, never as current Evidence items. They never establish or confirm a Finding | Old material cannot pose as current evidence, and its provenance stays checkable | CI-tested: `tests/pipeline/test_t8_memory.py::test_memory_findings_become_historical_claims_with_original_lineage`, `tests/pipeline/test_t8_historical_checks.py` |
| **Optional Run token cap.** `RUN_TOKEN_CAP` (none by default) fails a Run whose Passage-reading calls exceed it | A hard stop on runaway Passage reading | CI-tested: `tests/pipeline/test_extraction_synthesis.py::test_a_run_over_its_token_cap_fails_with_the_reason`. Planner, Researcher and Writer calls are not counted, so it does not bound all model spend (design section 10) |

### Run-log compaction: what is and is not proven

DeepAgents compacts an agent's history automatically, and each compaction is recorded in the
Run log as a `context_compacted` event (issue #60). The evidence comes at three levels:

| Level | Status | Evidence |
|---|---|---|
| (a) Compaction middleware configured | **Implemented (by the library).** `agents/planner.build_planner` calls `deepagents.create_deep_agent`. In the installed `deepagents` 0.7.23, that always appends `SummarizationMiddleware` to the Planner's stack and to each subagent's. If the chat model publishes a context-window profile, compaction triggers at 85% of it. Otherwise it triggers at 170,000 tokens and keeps the last 6 messages. For the default Planner models that means: Anthropic `claude-haiku-5-5`, no profile, so 170,000 tokens; Gemini `gemini-3.5-flash`, 85% of 1,048,576; Groq `openai/gpt-oss-120b`, 85% of 131,072. InsightForge does not change these thresholds | Read from the installed library source, and the profiles were read offline from the configured models; [`docs/t17-verification.md`](docs/t17-verification.md) |
| (b) Each compaction recorded in the Run log, and tested | **Implemented and CI-tested (forced, offline).** `agents/compaction.CompactionRecorder` runs after the summarization middleware on the Planner and on every subagent, and reports each completed compaction. `pipeline/graph.py` writes it once per `compaction_id` to `run_events`. The tests make the **real** middleware compact by giving scripted models a small context-window profile, which is how deepagents sets its own trigger. No event is faked | `tests/pipeline/test_compaction_events.py`: Planner compactions, a later compaction recorded and none twice, a restart on the same checkpoint, a Run resumed after its SQLite repositories and checkpointer were closed and reopened from disk, Researcher attribution per Sub-task, durability in a reopened SQLite file, and nothing recorded at the normal thresholds. `tests/test_deepagents_facts.py` pins the library state contract the recorder reads |
| (c) A real Run on a live model whose log shows a compaction | **Not observed. Not yet verified.** No live Run has been made since the recorder was added, and none is expected to reach the trigger (below) | `docs/live-runs/` (T3 to T5, all before issue #60) |

Persisted Run log from the forced offline scenario. The Planner has a 12,000-token profile and
padded replies, and the Run includes a repair round. This was read back from the SQLite file
on a new connection, with stage and repair events kept and the rest elided:

```
run_id=run_043206664f7e state=COMPLETE planner_model_calls=6 summary_calls=2
seq=1  stage              {"state": "PLANNING"}
seq=3  stage              {"state": "RESEARCHING"}
seq=4  context_compacted  {"agent": "planner", "compaction_id": "cmp_a43eab4206a26351", "cutoff_index": 4, "history_file": "/conversation_history/session_c0bb905a4d684b25a832929431d4e664.md", "messages_in_conversation": 6, "subtask_id": null, "summary_chars": 331}
seq=9  repair             {"missing": ["s3"]}
seq=10 context_compacted  {"agent": "planner", "compaction_id": "cmp_2d7df507cb973dae", "cutoff_index": 10, "history_file": "/conversation_history/session_c0bb905a4d684b25a832929431d4e664.md", "messages_in_conversation": 12, "subtask_id": null, "summary_chars": 331}
seq=13 stage              {"state": "EXTRACTING"}
...
seq=38 stage              {"state": "COMPLETE"}
```

How to read an event:

- `cutoff_index` counts the conversation's messages compacted so far.
- `messages_in_conversation` is the conversation's full history. The middleware summarises
  only what the model is sent, and the stored messages stay intact.
- `history_file` is where deepagents offloaded the compacted messages. It is a path in the
  agent's checkpointed file state, not a file on disk.
- `subtask_id` names the Sub-task a subagent was dispatched for.
- The event carries no message text.

Reproduce it with
`uv run pytest tests/pipeline/test_compaction_events.py -k durable -s`. That test prints this
log and asserts its shape: two compactions at cutoffs 4 and 10, one on each side of the repair
round. Run, compaction and session IDs differ on each run.

Why compaction is not expected in normal Runs: the boundaries above keep the Planner thread
small. Besides the DeepAgents system prompt, its thread holds the Brief with at most three Run
summaries, the plan, one canonical `task` call and one short result per Sub-task (at most 6),
and any correction or repair messages. The only Planner request size on record is 8,172 tokens: a
repair request that Groq's free tier rejected (design §12). That one measurement is about 5%
of the 170,000-token trigger and about 7% of Groq's. Thread sizes on other Runs have not been
measured, so this is an expectation, not a proof. Compaction is a backstop that the context
boundaries are designed never to reach. The forced scenario above pads the replies and shrinks
the window precisely because a real Run would not get there.

Limits:

- The recorder reads deepagents' private `_summarization_event` state key, through the
  library's exported `SUMMARIZATION_EVENT_KEY`. No public hook reports a completed compaction
  in 0.7.23. A library upgrade that changes the key or the event's fields fails
  `tests/test_deepagents_facts.py`.
- A compaction is identified by its agent, the id of its conversation's first message, its
  cumulative `cutoff_index` and its summary text. A compaction that summarises only the
  previous summary keeps the same `cutoff_index`, so the summary text tells the two apart.
  Two compactions with the same cutoff and byte-identical summaries would be recorded once.
  This is a known, accepted exception (no public compaction callback exists to do better).
- Not covered by a test: the Planner's correction and clarification resumes, the
  context-overflow fallback compaction, Doc Reader and Memory researcher attribution, and a
  subagent rerun after a crash. They use the same recorder and identity as the tested paths.
- Retention removes `context_compacted` events with the other detailed events of an expired
  Run.

## Evaluation

T16 added a five-Brief sample library (`samples/briefs/`) and an `eval` command that runs it.

| Brief ID | Industry and question type | Sources |
|---|---|---|
| `document-annual-report` | Medical devices: company deep dive from an uploaded annual report (fictional Northwind Health PDF) | documents |
| `memory-prior-report` | Electric vehicles: follow-up on an earlier Report (a setup Run for the same User comes first) | web and memory |
| `competitive-landscape-cloud` | Cloud: competitive landscape across named companies | web |
| `regulatory-impact-semiconductors` | Semiconductors: regulatory impact on named companies | web |
| `market-sizing-solar` | Solar: market sizing with an outlook | web |

```bash
uv run insightforge-agent eval --provider gemini --judge-provider none            # all five
uv run insightforge-agent eval --provider anthropic --judge-provider anthropic --brief market-sizing-solar
```

**This makes real, possibly paid, model and search calls.** It is never run by the test
suite, and it needs the keys from [Configuration](#configuration). `--provider` is repeatable
(`anthropic`, `gemini`, `groq`). `--judge-provider` is one provider, or `none` to skip the
synthesis judge. `--brief` is repeatable, and a run with it is marked partial. `--out` defaults
to `eval-results/`, which is git-ignored. Settings and every provider's pipeline are validated
before any Run starts: a usage or configuration error exits 2, and a pipeline, judge or
evaluation directory that cannot be set up exits 1. Each evaluation gets a new directory,
`<out>/<UTC timestamp>/` (`<timestamp>-1`, `-2` ... if one started in the same second), and
never reuses an existing one. Each provider gets its own SQLite files and an in-process Qdrant
under `<provider>/` in it, and each sample runs as its own User. The command writes
`eval-report.md` and then `eval-report.json` there, so `eval-report.json` exists only for a
complete evaluation. If the Runs finish but the reports cannot be written, it exits 3, leaves
no `eval-report.json`, and keeps the Runs' SQLite files.

What the measurements mean:

- **Fact-check pass rate** = `verified / (verified + unverified)` over the persisted verdicts
  of the sampled Claims. `unchecked` (the re-fetch failed) is excluded. The rate is undefined
  when no Claim could be checked. It is cross-checked against the stored draft, and any
  disagreement is a metric error, never a score.
- **Citation accuracy**: the share of Claims whose citation resolves and whose primary Passage
  states every figure of the Claim. It measures provenance and figure grounding, **not**
  semantic entailment.
- **Synthesis quality**: one LLM-judge call per Report with the fixed `synthesis-rubric-v1`
  rubric. It scores Coverage, Organisation, Synthesis and Calibration from 1 to 5, and the
  score is their mean. The judge sees the Brief, the expected structure and the draft, never
  Passage text. It is **not** evidence of factual accuracy.
- **Comparability** between providers is decided per metric. Two providers are comparable on a
  metric only when their pipeline conditions and runtime services (embedder, search, fetcher)
  match and the metric was measured on the same Briefs.
- **Anthropic remediation rule** (design §11): this applies only to an Anthropic run of the full
  library. If every Brief has a defined pass rate and the pooled rate is under 80%, the status
  is `required`, meaning a documented remediation attempt (owned by T18). If every Brief has a
  defined pass rate and the pooled rate is 80% or more, it is `not_required`. A missing, failed,
  undefined or partial measurement makes it `undetermined`.

Limits: one Run per Brief per provider, so there is no repeatability estimate. The Briefs'
`checks` are descriptive, and no metric enforces them. The scripted tests
(`tests/evaluation/`) show orchestration, persistence, subject relevance and provenance across
the five domains **within a scripted world** (the scripted models copy Passage sentences). They
do not show real-model research quality. **The harness is implemented. No real Anthropic
evaluation has been run yet. That is T18.**

## Security, privacy and limitations

- **Persona selection is not authentication.** See the note at the top. The backend scopes
  every request and record to the persona in a signed HttpOnly cookie (HMAC-SHA256,
  SameSite=Lax). Another persona's data returns 404. But anyone who can reach the API can
  select any persona. Do not expose this deployment to untrusted networks.
- **Share links are bearer capabilities.** They are HS256 JWTs carrying a unique share ID, the
  Report ID and an expiry (7 days by default). Revocation is stored in SQLite and survives a
  restart. Anyone holding the link can read the Report until it expires or is revoked. The
  public view exposes only the fact-checked Report, its fact-check summary and cited web
  Source metadata. It does not expose uploaded-PDF Passage text, Run events, internal Evidence
  objects or other Reports. Set `SHARE_JWT_SECRET` to separate its signing key from
  `SESSION_SECRET`; when blank, the demo deliberately reuses `SESSION_SECRET`.
- **Extraction and judgements are LLM-based.** Entities, structured facts, Evidence drafts,
  entailment verdicts, Finding normalisation for predicates outside the vocabulary, and
  synthesis scores all come from models. The deterministic checks narrow what a model can slip
  through. Every figure and named entity in an Evidence item must appear in its Passage.
  Structured values and stated periods must be stated in the text. Citations must resolve to
  stored Passages. Writer Claims may use only their item's figures and names. These checks are
  lexical. They do not prove that a value belongs to the entity it is attributed to, that two
  differently written names are the same entity, or that a paraphrase means the same thing.
  Confidence reflects source count and credibility, not truth.
- **Fact-checking is a sample.** It checks 20% of Claims (at least 5). A `verified` Claim had a
  supporting span at or above the 0.85 similarity threshold, containing the Claim's figures, that the
  light model judged SUPPORTED. Unsampled Claims are not checked, and `[UNVERIFIED]` means only
  that the check found no support.
- **Data handling.** Everything is stored locally: SQLite files (`*.db`, git-ignored), Qdrant,
  and the model cache. Fetched web pages and uploaded PDFs are kept for as long as a Report
  cites them. Other raw material is swept after `RETENTION_DAYS` (default 30). Reports are never
  deleted, and there is no delete endpoint. Brief text and Passage text are sent to the
  configured chat provider. Do not upload documents you cannot share with that provider.
- **Not production.** The app is one process on one node: no Postgres, no worker queue, no
  sign-up or credentials, no multi-process deployment. Search uses Tavily only (other listed providers are
  skipped). Hardening observations are tracked in issue #49 (T20).

## Anthropic verification results (T18): pending

**Status: pending. No results are reported here yet.** T18 (issue #19) is the final
verification on Anthropic. It will run the live paths and actual document and memory Runs,
re-run the T1 spike criteria, run the five-Brief evaluation and record the remediation decision
under the rule above. This README will be updated with those results, with traceable Run IDs and
the `eval-report` files, once T18 is done. Until then, T17's third acceptance criterion is open.

Earlier live runs on `claude-haiku-5-5` were single development runs, not the T18 verification:
the T1 spike (design §12), the T3 to T5 runs in [`docs/live-runs/`](docs/live-runs/), and the
F2 Fact-Checker cases (16 of 16, design F2 notes).
