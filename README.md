# InsightForge

An autonomous research platform: a user submits a research question (a **Brief**) and receives
a cited intelligence **Report**. Standing Briefs can be re-researched on a schedule, and
material changes are surfaced as **Alerts**.

**Demo video (6 min 19 s):** [watch or download the MP4](https://github.com/subhadlearner/insightforge-agent/releases/download/demo-2026-10-21/InsightForge-Demo-2026-10-21.mp4), from the [demo release](https://github.com/subhadlearner/insightforge-agent/releases/tag/demo-2026-10-21).
It follows one live Run through the web UI, from Brief to cited Report, export and Alerts
([what the Run did and did not establish](#live-end-to-end-runs-after-adr-0006)).

> **The persona picker is identity selection for the demo. It is not authentication and must
> not be treated as a security boundary.** Anyone who can reach the API can select any persona
> in `users.yml`. The session cookie is signed (HMAC with `SESSION_SECRET`) and HttpOnly only so
> that a browser cannot switch persona by editing it; there are no passwords and no sign-up.

Vocabulary is defined in [`CONTEXT.md`](CONTEXT.md), the design in
[`docs/design.md`](docs/design.md), and hard-to-reverse decisions in [`docs/adr/`](docs/adr/). [`docs/README.md`](docs/README.md)
indexes every document, current and historical.
This README uses those terms exactly (Source, Observation, Passage, Evidence item, Claim,
Finding, Historical Claim, Baseline, Alert, Run summary).

**How claims in this README are labelled.** *Implemented* means the code does it.
*CI-tested* means an offline test in the default suite asserts it. *Locally exercised* means
it was run by hand and the record is linked. *Design-specified* means `docs/design.md` requires
it. *Not yet verified* means nobody has shown it on a real Run. The fresh-clone record behind
the setup and test sections is [`docs/t17-verification.md`](docs/t17-verification.md).

## Status at a glance

This describes the code as of ADR-0006 (PR #77). Open work is tracked in
[GitHub issues](https://github.com/subhadlearner/insightforge-agent/issues).

| Area | Status |
|---|---|
| Pipeline, API, web UI, Watchlists, Diffs/Alerts, export, Share links, retention, eval harness | Implemented and CI-tested offline (scripted models, recorded search) |
| Live end-to-end Runs on Anthropic (Claude Haiku 5.5) after ADR-0006 | **Three COMPLETE Runs**: two budget-capped harness Runs and one Run through the web UI, recorded on video ([Live end-to-end Runs](#live-end-to-end-runs-after-adr-0006)) |
| Formal T18 Anthropic verification (#19) | **Historical gates FAILED and stay failed**: B-1 FAIL, Stage C FAIL, Stage D not run ([T18 results](#anthropic-verification-results-t18-historical)) |
| ADR-0006 live support gate (Gate A) | **FAILED** on its one attempt; not re-run |
| Follow-up tickets | T17 README (#18), T18 verification (#19), T19 demo post (#20), T20 hardening (#49), post-submission research quality (#75, #76) |

## Contents

1. [What InsightForge does](#what-insightforge-does)
2. [Quick start](#quick-start)
3. [Features](#features)
4. [Architecture](#architecture), including the [repository layout](#repository-layout)
5. [Getting started from a fresh clone](#getting-started-from-a-fresh-clone)
6. [Running it](#running-it), including the [UI walkthrough](#ui-walkthrough)
7. [Testing](#testing)
8. [Context engineering and evidence](#context-engineering-and-evidence)
9. [Evaluation](#evaluation)
10. [Research-integrity redesign (ADR-0006)](#research-integrity-redesign-adr-0006)
11. [Live end-to-end Runs (after ADR-0006)](#live-end-to-end-runs-after-adr-0006)
12. [Anthropic verification results (T18, historical)](#anthropic-verification-results-t18-historical)
13. [Security, privacy and known limitations](#security-privacy-and-known-limitations)

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
(T16). Since T18, ADR-0006 rebuilt how Evidence is supported, tracked against the Brief's
questions and checked before publication ([Research-integrity redesign](#research-integrity-redesign-adr-0006)).

## Quick start

**Offline, no keys, no paid calls:**

```bash
git clone https://github.com/subhadlearner/insightforge-agent.git
cd insightforge-agent
uv sync --locked
uv run insightforge-agent demo        # real Extraction and synthesis, scripted model, PASS/FAIL list
uv run pytest                         # default offline suite; outbound network is blocked
cd src/frontend && npm ci && npx playwright install chromium && npm run test:e2e   # the whole app in a browser, scripted models
```

**The real app** (makes real model and search calls):

```bash
cp .env.example .env      # set SESSION_SECRET (32+ chars), LLM_PROVIDER and its key, TAVILY_API_KEY
docker compose up -d qdrant                      # or set QDRANT_URL=:memory: in .env
uv run insightforge-agent serve --port 8000      # terminal 1
cd src/frontend && npm run dev                   # terminal 2, then open http://localhost:3000
```

Details, prerequisites and troubleshooting follow in
[Getting started](#getting-started-from-a-fresh-clone) and [Running it](#running-it).

## Features

| Feature | Where | Ticket |
|---|---|---|
| Brief to cited Report through a bounded multi-agent pipeline (Planner, isolated Researchers, Extraction, Synthesis, Writer, Fact-Checker) | `pipeline/`, `agents/` | T3 to T6 |
| Three source types per Sub-task: web (Tavily), uploaded PDFs (Docling + Qdrant), the User's earlier Reports | `agents/web.py`, `agents/doc_reader.py`, `agents/memory_researcher.py` | T7, T8 |
| One clarifying question for a Vague Brief, with a durable timeout and resume after restart | `pipeline/clarification.py`, `scheduler/clarification.py` | T9 |
| HTTP API, persona session, live SSE progress with `Last-Event-ID` resume | `api/` | T10 |
| Watchlist Items with `max_runs`, Baselines, Findings, Diffs, Alerts and an optional Slack-format webhook | `api/watchlist.py`, `scheduler/`, `domain/diff.py` | T11, T12 |
| Retention sweep for raw material; Reports are kept | `scheduler/retention.py` | T13 |
| Next.js workbench: Brief input, runtime estimate, live progress, Report viewer, citation drawer | `src/frontend/` | T14 |
| Watchlist and Alerts pages, Markdown and PDF export, revocable Share links | `src/frontend/`, `api/delivery.py` | T15 |
| Sample Brief library and the `eval` command (fact-check rate, citation accuracy, synthesis judge) | `samples/briefs/`, `evaluation/` | T16 |
| Evidence-bound support judgment, one Evidence pipeline, bounded work-unit recovery, requirements ledger, checks before publication | `domain/support.py`, `domain/requirements.py`, `pipeline/` | ADR-0006 (#75, #76) |

## Architecture

```
Browser ── Next.js (src/frontend/) ──/api/* rewrite──> FastAPI (api/) ── RunExecutor (bounded threads)
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

| Package (`src/backend/insightforge_agent/`) | Responsibility |
|---|---|
| `domain/` | Pydantic stage contracts and pure rules: plan bounds, periods, quantities, Confidence, Findings, Diff. No I/O, and it imports no other package |
| `pipeline/` | The LangGraph outer graph (`graph.py`), with one module per stage: `ingest`, `research`, `extract`, `synthesize`, `write`, `fact_check`. Also citations, memory, recovery and the bounded `RunExecutor` |
| `agents/` | Prompts and DeepAgents definitions: the Planner (`create_deep_agent` root), the web Researcher, the Doc Reader, the Memory researcher and the Finding judge |
| `stores/` | `SourceStore` (the only way to reach Passage text), SQLite repositories, and the Qdrant collections `doc_chunks` and `report_sections` |
| `api/` | FastAPI routers, persona session, SSE progress, export and share delivery |
| `scheduler/` | APScheduler jobs: Watchlist ticks, Diff, webhook delivery, clarification timeout, retention |
| `evaluation/` | The `eval` command, metrics, the synthesis judge and the report writer (T16) |
| `benchmark/` | The offline Evidence-quality benchmark tools (`insightforge-agent bench`) |

### Repository layout

```
insightforge-agent/
├── pyproject.toml, uv.lock        one Python project; run uv and the CLI from here
├── users.yml, .env.example        personas and settings, read from the working directory
├── docker-compose.yml             the optional Qdrant server
├── src/
│   ├── backend/
│   │   ├── insightforge_agent/    the Python package (uv_build module-root = src/backend)
│   │   └── tests/                 pytest suites, fixtures and the scripted browser-E2E backend
│   └── frontend/                  the Next.js app, its Vitest and Playwright tests, render-pdf.mjs
├── samples/                       the five sample Briefs and the fictional Northwind PDF (eval inputs)
├── benchmarks/evidence/           the 48-case Evidence-quality benchmark and the owner review record
└── docs/                          design, ADRs, audits and verification records (see docs/README.md)
```

Run `uv`, `pytest` and `insightforge-agent` from the repository root: the CLIs default to
root-relative `samples/briefs`, `benchmarks/evidence` and `users.yml`. Run `npm` commands in
`src/frontend/`. The Python import name stays `insightforge_agent` and the command stays
`insightforge-agent`. The Northwind Health annual report is **fictional** test material and is
never evidence about a real company.

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
cd src/frontend && npm install && cd ../..                      # web UI dependencies
cd src/frontend && npx playwright install chromium && cd ../..  # optional: PDF export and browser tests
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

Stop it with Ctrl+C. In order: new Runs are refused from that moment (503, and no Watchlist
tick starts one), then open progress streams end (clients reconnect with `Last-Event-ID`); the
scheduler is paused; queued new Runs are settled FAILED and queued clarification resumes stay
RESUMING for the next start; running Runs are waited for with every resource still open; only
then is the scheduler shut down and are Qdrant, the checkpointer and the databases closed
(design.md, T10 notes). Shutdown waits for running Runs with no time limit, so a stalled Run
keeps the process from exiting; after a hard kill, startup handles each unfinished Run by its
stored state (resuming or rescheduling clarifications, settling the rest). There is no Run or
Sub-task timeout (design.md section 10). Plain `uvicorn insightforge_agent.api.app:app_from_env --factory` serves
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
| `GET /research/{run_id}/timing` | the Run's stored `created_at` and the server's clock, for the Run page's elapsed time; read only |
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

The Next.js app in `src/frontend/` is the research workbench: pick a persona, write a Brief, choose
sources and attach PDFs, see the runtime estimate, follow the Run live (stage rail, each
Sub-task's state, workflow milestones, elapsed time and an activity log), answer a clarifying
question, and read the Report with a table of contents and a citation drawer for every Claim. Run the API as above,
then in a second terminal:

```bash
cd src/frontend
npm run dev
```

Open http://localhost:3000 (use `localhost`: `next dev` blocks its development assets for
other host names such as `127.0.0.1`). The browser only talks to Next; `/api/*` is rewritten
to FastAPI, so the persona cookie and the live progress stream are same-origin and no CORS is
configured. `INSIGHTFORGE_API_ORIGIN` (default `http://127.0.0.1:8000`) tells the Next server
where FastAPI is; `next dev` reads it at start, and `next build` bakes it into the build that
`next start` serves.

### UI walkthrough

This is the path shown in the recorded demo of the Qdrant vs Weaviate Brief
([Live end-to-end Runs](#live-end-to-end-runs-after-adr-0006)):

1. **Pick a persona** (`/`). The picker lists the personas in `users.yml` and sets the signed
   cookie. It selects an identity and is not a login.
2. **Write the Brief and choose sources.** Toggle web, documents and memory, and attach up to
   10 PDFs. The runtime estimate updates before you submit (`POST /research/estimate`).
3. **Follow the Run** (`/runs/{runId}`). The stage rail moves from PLANNING to COMPLETE. Each
   Sub-task shows its query and state. Workflow milestones, the elapsed time and an activity
   log stream over SSE. A Vague Brief stops here with one clarifying question to answer.
4. **Read the Report** (`/reports/{reportId}`). It has a table of contents, sections with a
   Confidence label, `[UNVERIFIED]` markings where the sampled check found no support, and a
   Gaps & limitations section built from the requirements ledger.
5. **Open the citation drawer** on any Claim. It shows the Source, its date and credibility, and
   the exact cited Passage. For a comparison it shows every premise.
6. **Export and share.** Download Markdown or PDF, or create a revocable Share link that opens
   at `/share/{token}` without a persona.
7. **Watch a topic** (`/watchlist`, `/watchlist/{itemId}`, `/alerts`). The UI lists Watchlist
   Items with their Run counts, shows each item's Diff history, and lets you read and mark
   material-change Alerts. Watchlist Items are created and edited through the API
   (`POST /watchlist`, below), not in the UI.

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

Only the model is scripted (`src/backend/insightforge_agent/demo.py`); every decision shown is made by
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
`src/frontend/scripts/render-pdf.mjs`, signs the owner's persona cookie for that browser context, and
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
| PDF export fails | Run `npx playwright install chromium` in `src/frontend/`, and keep the Next app running at `WEB_ORIGIN` |
| `pytest -m integration` reports skips | Model files are not cached. Run `uv run insightforge-agent models download` once |
| Loopback HTTP tests fail behind a proxy | Unset `ALL_PROXY`/`HTTP(S)_PROXY` for the test process (see [`docs/t11-verification.md`](docs/t11-verification.md)) |

## Testing

| Suite | Command (from the repo root unless noted) | Needs | In CI |
|---|---|---|---|
| Default offline suite (tiers 1-3 and everything not `live` or `integration`) | `uv run pytest` | nothing; outbound network is blocked by `src/backend/tests/conftest.py` | yes, split by tier |
| One tier | `uv run pytest -m baseline` / `replay` / `scripted` | nothing | yes |
| Integration (real Docling, FastEmbed, in-memory Qdrant) | `uv run pytest -m integration` | model files from `models download`; runs offline and **skips** without them | the step runs, but CI has no model cache, so these tests skip there |
| Live (real model or server) | `INSIGHTFORGE_ALLOW_LIVE_TESTS=1 uv run pytest -m live` | real keys; **makes paid calls** | never |
| Import contracts | `uv run lint-imports` | nothing | yes |
| Frontend unit and component tests (Vitest) | `npm test` in `src/frontend/` | `npm install` | yes |
| TypeScript | `npm run typecheck` in `src/frontend/` | `npm install` | yes |
| Production build | `npm run build` in `src/frontend/` | `npm install` | yes |
| Browser E2E (Playwright) | `npm run test:e2e` in `src/frontend/` | `npx playwright install chromium` and `uv`. Starts `src/backend/tests/web_e2e/server.py` (the real app, scripted models, no keys, every outbound connection blocked) and `next start` itself | yes |
| Real-PDF browser E2E (part of the above) | `npx playwright test e2e/real-pdf.spec.ts` in `src/frontend/` | model files from `models download`. Uploads the sample PDF through the browser; the real Docling, FastEmbed and in-process Qdrant read, index and retrieve it, and the citation drawer must show the cited passage with its page and section. Only chat models are scripted. **Skips** with the reason when the model cache is missing | the step runs, but CI has no model cache, so this test skips there |
| Generated API types are current | `npm run api:types` in `src/frontend/` against a running API, then `git diff` | a running API | yes |

In PowerShell, set the variable first: `$env:INSIGHTFORGE_ALLOW_LIVE_TESTS = "1"; uv run pytest -m live`
(it stays set for that session; `Remove-Item Env:INSIGHTFORGE_ALLOW_LIVE_TESTS` clears it).
`pytest -m live` alone is not enough: live tests also need `INSIGHTFORGE_ALLOW_LIVE_TESTS=1`,
and skip without it.

Results after the repository restructure (branch `chore/repository-layout-cleanup` at
`dce952a`, cloned into an empty directory), 2026-10-22, Windows 11, uv 0.12.5, Node 24.20.0.
Caches were warm, there was no `.env` and no paid call. The previous record (`main` at
`32b0fc4`) is in [`docs/t17-verification.md`](docs/t17-verification.md).

| Check | Result |
|---|---|
| `uv sync --locked`, `uv build` | exit 0; the wheel holds the 113 modules of `insightforge_agent` and no tests, and installs and imports in a clean venv |
| `uv run lint-imports` | 5 kept, 0 broken |
| `uv run insightforge-agent demo` | exit 0, 7 of 7 expectations PASS |
| `uv run pytest` | **2,554 passed**, 41 deselected (12 min 30 s): the previous 2,550 plus 4 new layout tests |
| `uv run pytest -m integration` | 19 passed, **12 skipped** (the T18 replays need `T18_EVAL_DB`/`T18_EVAL_DIR`, kept outside the repository; the passes depend on the warm local model cache, which CI lacks) |
| `npm ci` / `npm test` / `typecheck` / `build` (in `src/frontend/`) | exit 0 / 205 passed in 14 files / clean / 8 routes |
| `npm run test:e2e` | **23 passed** (4.0 min), including the real-PDF test and PDF export through the real renderer |
| `npm run api:types` against the scripted backend | `lib/api-schema.d.ts` unchanged |

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
| **Store by reference.** Sources, Observations and Passages live in SQLite (PDF chunks also in Qdrant) behind `SourceStore`. Graph state and checkpoints carry IDs and short structured values, never page bodies | A 20,000-character page costs the graph nothing. Only a stage that needs the text loads it | CI-tested: `src/backend/tests/pipeline/test_happy_path.py::test_graph_state_carries_no_source_body` and `src/backend/tests/pipeline/test_happy_path.py::test_no_source_body_in_any_checkpoint_or_intermediate_state` (a sentinel in a 23k-character page never appears in state or in either checkpoint) |
| **Researchers see summaries, not pages.** The fetch tool writes each Source's one-line summary in its own bounded call. The Researcher sees only `{source_id, title, summary}` | Five full articles per search would otherwise sit in the Researcher's context ([design rationale Q18](docs/design-rationale.md)) | CI-tested: `src/backend/tests/pipeline/test_isolation.py::test_each_researcher_receives_only_its_own_subtask` |
| **Isolated Researchers.** Each Researcher is a DeepAgents subagent with its own `tools=` and only its Sub-task's task description. It does not see the Brief, the plan or its peers. Only its final message returns to the Planner | The Planner's thread grows by one short result per Sub-task, not by every search and fetch | CI-tested: same test, plus `src/backend/tests/pipeline/test_isolation.py::test_the_researcher_subagent_has_its_own_tools_and_is_not_forked`. Isolation on a real model was checked in the T1 spike (`docs/design.md` section 12) |
| **Planner memory is three Run summaries.** The Planner gets the last 3 Run summaries for the same Watchlist Item, or for the same User's one-shot Runs. Each summary is bounded (Brief 500 characters, 6 themes, 12 entities, 6 gaps). It never sees transcripts, earlier Reports or Passage text | Memory stays constant in size however many Runs a User has made | CI-tested: `src/backend/tests/pipeline/test_memory_research.py::test_the_planner_reads_exactly_the_newest_three_summaries_and_no_transcript`. The Run log records `run_summaries_loaded` with count and IDs only |
| **Bounded Passage batches.** Extraction (one Source at a time), the support judgment (one Evidence item or sampled Claim at a time) and the Fact-Checker read Passage text in batches under `PASSAGE_TOKEN_CAP` (default 3,000 estimated tokens, measured on the call's user message). An oversized Passage is read in windows, never truncated | The cost of one call does not grow with page or document length | CI-tested: `src/backend/tests/pipeline/test_extraction_synthesis.py::test_synthesis_payloads_respect_the_per_call_cap_separately_from_the_bundle_budget` (captures the payloads); `src/backend/tests/pipeline/test_extraction_synthesis_edge_cases.py::test_an_oversized_passage_is_read_in_bounded_windows_by_extraction_and_synthesis` |
| **Structured output and bounded recovery (T18, ADR-0006).** Extraction, the only call that proposes facts since ADR-0006, asks for the provider's native structured output where one exists (Anthropic: `output_config` JSON schema; `DRAFT_STRUCTURED_OUTPUT`, default on); `DRAFT_PASSAGE_CAP` (default off) can bound the Passages per call. A truncated, invalid or rejected batch is split and retried at most two levels deep within `WORK_UNIT_RETRY_CAP` (default 12) per Run; what stays unread is reported as a gap, never read as "no facts" | In the T18 evaluation 37 of 144 drafting calls failed (26 malformed JSON, 11 cut off), and the Stage C regulatory Run lost 5 drafting and 2 extraction batches at the output limit plus 1 invalid reply | CI-tested: `src/backend/tests/test_llm_json.py`, `src/backend/tests/pipeline/test_extraction_synthesis.py::test_only_extraction_uses_the_native_structured_path`, `src/backend/tests/pipeline/test_extraction_work_unit_recovery.py` |
| **Evidence budget for the Writer.** The Writer gets a ranked Evidence bundle under `EVIDENCE_BUDGET_TOKENS` (default 6,000) plus the Historical context and a compact requirement index (ADR-0006, outside the budget), and never sees Passage text. When the bundle is over budget, lower-ranked items are first shown in a shorter wording (same id and citation), each logged as `evidence_compressed`, but only when the support contract judges the wording equivalent to the statement both ways; otherwise the item stays whole (`evidence_compression_refused`); only if that is not enough are whole lowest-ranked items cut (never truncated mid-item), never the last kept support of a required research requirement while anything else can go, each logged as `evidence_cut` with its reason, Confidence, rank and requirements | The Writer's input is bounded however much research succeeded | CI-tested: `src/backend/tests/pipeline/test_extraction_synthesis.py::test_bundle_respects_the_budget_and_every_cut_is_logged`, `src/backend/tests/pipeline/test_evidence_compression_and_completeness.py::test_the_bundle_is_shortened_before_anything_is_cut`. Shortening costs a light call and two support judgments per item; only redundancy can be removed (a wording that drops content is not equivalent), so most over-budget items are still cut, and a requirement whose only verified Evidence was cut is reported as left out for the budget. Historical items are bounded by search k = 5 but are not counted in the 6,000 budget (T8 limitation) |
| **Historical context stays apart.** Claims from earlier Reports enter as Historical items with their original Report, date and Passages, never as current Evidence items. They never establish or confirm a Finding | Old material cannot pose as current evidence, and its provenance stays checkable | CI-tested: `src/backend/tests/pipeline/test_memory_research.py::test_memory_findings_become_historical_claims_with_original_lineage`, `src/backend/tests/pipeline/test_historical_claim_checks.py` |
| **Optional Run token cap.** `RUN_TOKEN_CAP` (none by default) fails a Run whose Passage-reading calls exceed it | A hard stop on runaway Passage reading | CI-tested: `src/backend/tests/pipeline/test_extraction_synthesis.py::test_a_run_over_its_token_cap_fails_with_the_reason`. Planner, Researcher and Writer calls are not counted, so it does not bound all model spend (design section 10) |

### Run-log compaction: what is and is not proven

DeepAgents compacts an agent's history automatically, and each compaction is recorded in the
Run log as a `context_compacted` event (issue #60). The evidence comes at three levels:

| Level | Status | Evidence |
|---|---|---|
| (a) Compaction middleware configured | **Implemented (by the library).** `agents/planner.build_planner` calls `deepagents.create_deep_agent`. In the installed `deepagents` 0.7.23, that always appends `SummarizationMiddleware` to the Planner's stack and to each subagent's. If the chat model publishes a context-window profile, compaction triggers at 85% of it. Otherwise it triggers at 170,000 tokens and keeps the last 6 messages. For the default Planner models that means: Anthropic `claude-haiku-5-5`, no profile, so 170,000 tokens; Gemini `gemini-3.5-flash`, 85% of 1,048,576; Groq `openai/gpt-oss-120b`, 85% of 131,072. InsightForge does not change these thresholds | Read from the installed library source, and the profiles were read offline from the configured models; [`docs/t17-verification.md`](docs/t17-verification.md) |
| (b) Each compaction recorded in the Run log, and tested | **Implemented and CI-tested (forced, offline).** `agents/compaction.CompactionRecorder` runs after the summarization middleware on the Planner and on every subagent, and reports each completed compaction. `pipeline/graph.py` writes it once per `compaction_id` to `run_events`. The tests make the **real** middleware compact by giving scripted models a small context-window profile, which is how deepagents sets its own trigger. No event is faked | `src/backend/tests/pipeline/test_compaction_events.py`: Planner compactions, a later compaction recorded and none twice, a restart on the same checkpoint, a Run resumed after its SQLite repositories and checkpointer were closed and reopened from disk, Researcher attribution per Sub-task, durability in a reopened SQLite file, and nothing recorded at the normal thresholds. `src/backend/tests/test_deepagents_facts.py` pins the library state contract the recorder reads |
| (c) A real Run on a live model whose log shows a compaction | **Not observed.** Three live Runs have been made since the recorder was added (`run_c11947913dc5`, `run_846d12a50177`, `run_e7499e34e1e6`), and none logged a `context_compacted` event, as expected (below). So the recorder has not been seen to fire on a live model | [Live end-to-end Runs](#live-end-to-end-runs-after-adr-0006); `docs/live-runs/` (T3 to T5, before issue #60) |

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
`uv run pytest src/backend/tests/pipeline/test_compaction_events.py -k durable -s`. That test prints this
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
  `src/backend/tests/test_deepagents_facts.py`.
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
uv run insightforge-agent eval-judge --from eval-results/<timestamp> --judge-provider anthropic  # re-judge stored Reports into a separate report
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
no `eval-report.json`, and keeps the Runs' SQLite files. An unexpected error while the Runs
execute (or Ctrl+C) stops it with a traceback and exit 1, also with no `eval-report.json`.

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
(`src/backend/tests/evaluation/`) show orchestration, persistence, subject relevance and provenance across
the five domains **within a scripted world** (the scripted models copy Passage sentences). They
do not show real-model research quality. The T18 Anthropic verification ran this harness: the
baseline five-Brief fact-check rate was 66.7%, and the later targeted Stage C acceptance gate
FAILED. See [T18 results](#anthropic-verification-results-t18-historical) for the limits. The
`eval` command has not been re-run since ADR-0006.

## Research-integrity redesign (ADR-0006)

Merged to `main` in PR #77. The T18 results below are unchanged and remain the formal record
(B-1 FAILED, Stage C FAILED, Stage D not run). This redesign addresses the causes T18 exposed
([ADR-0006](docs/adr/0006-evidence-bound-support-and-requirements-ledger.md), #75, #76):

- **Evidence-bound support.** Synthesis, the Writer's checks and the Fact-Checker share one
  judgment that must show, for every part of a statement, the exact words of the cited Passage
  that state it; code verifies that the quotes occur in the Passage, that the parts cover the
  statement and that its figures are stated. No lexical name rule
  refuses a candidate before the judge (the rule that lost Stage C's six-programmes fact is
  retired), and an uploaded document's company is given to the judge as identity context only.
- **One Evidence pipeline.** Extraction proposes every fact once, anchored to its Passage;
  Synthesis verifies and ranks those facts instead of drafting a second, different set.
- **No silent loss.** Truncated or invalid model batches are split and retried within bounds;
  what stays unread is reported as a gap.
- **Requirements ledger.** The Brief's atomic questions are tracked to Evidence and Claims; an
  item counts for a question only once a check confirms it answers it; what was not
  established, could not be checked, or failed the Fact-Checker becomes a gap line, so "Gaps &
  limitations" matches what is missing.
- **Checked before publication.** A Claim that rewords its Evidence is judged against the cited
  Passage, and a summary sentence against the published Claims. What fails is omitted with the
  reason logged, never replaced by the bare Evidence statement. A comparison may cite several
  items, is checked against all of them, and the citation drawer shows every premise.

What is proven offline, and what is not, is in the
[verification record](docs/audits/2026-10-21-research-redesign-verification.md). Scripted-model
tests show the code keeps every gold positive (including all 13 Stage C Northwind facts end to
end) and refuses every negative it can refuse on its own. They cannot show how reliably a live
model judges qualifiers, modality or attribution.

## Live end-to-end Runs (after ADR-0006)

Every live Run below used Anthropic Claude Haiku 5.5 for all three roles (planner, writer,
light) and Tavily search, and finished **COMPLETE** with one Report. These are individual Runs,
not an acceptance gate: they show the system working end to end on a real model, and they
show where Report quality still falls short. They do not replace or overturn the T18 results.

| Run | How | Brief | Outcome | Record |
|---|---|---|---|---|
| Gate A (support judgment only, not a Run) | owner-authorised harness, $0.50 cap | the B-1 gold Claims | **FAILED**, $0.0675: 8 of 9 wrong rejections were code vetoes (since retired); the model made 1 wrong acceptance and 1 wrong rejection. Not re-run | verification record §13 |
| `run_c11947913dc5` | budget-capped harness ($1.00 cap, 20 searches), on `b31ffc2` | BYD vs Tesla BEVs: 2025 sales, pricing, battery technology | COMPLETE in 43 min, **$0.317**, 711 model calls, 7 searches. The Report marked all 6 requirements as not established and 25 Claims were restated verbatim. Both causes were fixed and pinned by tests | verification record §14 |
| `run_846d12a50177` | same harness, on `9ddf640` | same Brief | COMPLETE in 52 min, **$0.385**, 852 model calls, 6 searches. All 6 requirements reported; 33 Claims, every citation resolves from a fresh connection; Fact-Checker sample 7/7 verified. It published a UK figure as Tesla's 2025 sales, and the BYD vs Tesla comparison was refused. Both were corrected afterwards (omission rule, footnoted-quote fix) and pinned by tests. The UI Run below ran the corrected code on a different Brief; this Brief has not been re-run | verification record §15 |
| `run_e7499e34e1e6` | **the web UI**, recorded on video; checkout at `09db8dd` per the git reflog (the code of `main` at `32b0fc4`: the two later commits changed only a test and generated types), Docker Qdrant | Qdrant vs Weaviate for a self-hosted RAG app (hybrid search, metadata filtering, Docker operations, a recommendation for a solo developer) | COMPLETE in 1 h 37 min (Synthesis took 69 min). 6/6 Sub-tasks succeeded, 27 Sources stored (15 cited in the Report), 57 Claims in 4 sections, Fact-Checker sample **12/12 verified**. Requirements ledger: 6 of 11 reported, 1 partial, 4 incomplete. **The recommendation was not established**: 5 extraction batches (56 and 309 Passages in two Sub-tasks) stayed unread after bounded retries (output truncated), and the Report says so in Gaps & limitations. Three of the four sections are LOW Confidence and one is MEDIUM; most cited Sources are undated (two have dates). Cost not measured (no harness on UI Runs) | below |

**The UI demonstration** (2026-10-21) drove `run_e7499e34e1e6` through the whole
[UI walkthrough](#ui-walkthrough): persona selection, Brief and estimate, live progress, the
Report with its citation drawer, Share-link listing, the Watchlist and Alerts pages, and
Markdown and PDF export (`rep_3f4e32cbd7af0109`). Read from the API log:

- Markdown export returned 200 on the first try.
- PDF export returned **502 twice** (`PdfRenderError: Chromium renderer failed`) while the
  `next dev` server was logging worker errors. It returned 200 after the web server was
  restarted.
- The Qdrant client (1.19.1) warned that it is incompatible with the pinned Docker server
  (1.12.4). Document search was not part of this Brief, so that path was not exercised against
  the Docker server.

The edited video ([`InsightForge-Demo-2026-10-21.mp4`](https://github.com/subhadlearner/insightforge-agent/releases/download/demo-2026-10-21/InsightForge-Demo-2026-10-21.mp4), 6 min 19 s, captioned, with
time-lapse speeds shown on screen) is a release asset, not a repository file. The raw recording,
the exported Report (Markdown and PDF) and the API and web logs stay outside the repository.
The demo post itself is T19 (#20).

What these Runs do **not** show: repeatability (one Run per Brief), whether the corrections
made after `run_846d12a50177` fix that Brief's Report, a re-run of the formal T18 gates, or the
uploaded-document and memory paths after ADR-0006 on a live model.

## Anthropic verification results (T18, historical)

This is the formal T18 record, kept as it was reported. It ran before ADR-0006, and nothing
later re-ran or overturned these gates: the live Runs above are separate evidence, not passes.

**Live verification completed; the acceptance gates did not all pass.** T18 (#19) exercised all
five sample Briefs on Anthropic (Claude Haiku 5.5) at baseline, including the real uploaded-PDF
Doc Reader path and the earlier-Report Memory researcher path. The full-library baseline
fact-check pass rate was **20/30 = 66.7%**, below the 80% remediation threshold; citations were
**115/115** accurate by the evaluation's provenance/figure checks. Remediation was attempted
and offline-tested, but its effect on the full five-Brief Anthropic rate was **not established**.

| Verification gate | Formal outcome |
|---|---|
| Planner spike criteria 1–3 | PASS, thin evidence accepted by the owner |
| **B-1: entailment calibration** | **FAIL** (three live attempts); documented exception, not a pass |
| B-2: structured drafting smoke test | PASS (39/47 calls parsed; 8 hit the output-token limit) |
| B-3: judge-only | PASS (5/5 baseline Reports judged with valid score sheets; mean 2.40/5) |
| **Stage C: targeted live end-to-end** | **FAIL** (acceptance exit 1; final attempt) |
| Stage D: remediated five-Brief re-evaluation | NOT RUN (stopped after Stage C) |

**B-1 limitation.** A cited-Passage entailment check can accept a market definition, financial
metric or qualifier stated elsewhere on the Source page but **not in the cited Passage**.
The B-1 gate failed despite an explicitly accepted exception; it was not corrected or retested
to a pass. See [B-1 audit and gate exception](docs/audits/2026-10-21-t18-b1-gate-exception.md)
and [follow-up #75](https://github.com/subhadlearner/insightforge-agent/issues/75).
A Fact-Checker `verified` verdict therefore does not guarantee every qualifier is directly
substantiated by its citation.

**Stage C final run.** Both targeted Briefs published Reports. The regulatory-impact Brief
covered **2/2** required objectives. The uploaded-document Brief covered **10/13** required
facts, with **2 candidates** (hospital pricing pressure and NW-200 delay risk) and **1 missing**
(six clinical-trial programmes). Consequently the **formal result remains FAILED**, not PASS
or merely INCONCLUSIVE. The two Reports had **14/14 sampled Claims verified** and
**65/65 citations accurate** under the recorded checks; these figures are not a full-Claim
fact-check or a substitute for the missing facts. Synthesis-judge scores were **2.75/5** and
**2.25/5**, respectively, reflecting unresolved comparisons and limited analytical synthesis.
See [post-submission follow-up #76](https://github.com/subhadlearner/insightforge-agent/issues/76).

**B-3 judge-only.** The five stored baseline Reports were judged again, without research, by
`claude-haiku-5-5` under `synthesis-rubric-v1` and `judge-prompt-2`: all five got a valid score
sheet (acceptance PASS), with a mean of **2.40/5** and **2/5 on synthesis for every Report**.
This shows only that the judge produced valid score sheets in this one run; it does not show
good Report quality, factual accuracy, repeatability or a successful remediation. The baseline's own judge result (2 judged, 3 errors)
stands unchanged and is not pooled with B-3. Its Anthropic usage and billing are **unknown**.

The Stage C attempts are preserved separately. Stage D was not run, so **there is no
verified remediated five-Brief Anthropic pass rate**. Actual Anthropic billing for Stage C
and T18 overall is **unknown**; harness estimates are not billed amounts.

The [T18 verification record](docs/t18-verification.md) is authoritative for run IDs, exact
gate results, archived evidence hashes and retrospective diagnostics (which never replace
formal results). The underlying evaluation artifacts are stored separately from the public
repository. These results were handed to [README ticket #18](https://github.com/subhadlearner/insightforge-agent/issues/18)
and [demo ticket #20](https://github.com/subhadlearner/insightforge-agent/issues/20);
they do not mean those tickets' other work is complete.

## Security, privacy and known limitations

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
- **Extraction and judgements are LLM-based.** Entities, structured facts, support verdicts,
  requirement answers, Finding normalisation for predicates outside the vocabulary, and
  synthesis scores all come from models. Since ADR-0006, support is a semantic judgment bound
  to the cited Passage: for a SUPPORTED verdict the model must return a support map that
  quotes, for each part of the statement, the exact words of the cited Passage that state it.
  Code then checks, and fails closed, that every quote occurs in the cited unit, that the parts
  cover the statement's content words, that every figure is stated by the quotes, and that an
  identity part is used only with document identity context (an uploaded document whose own
  text names its company). Citations must resolve to stored Passages.
  - **What code does not decide.** Whether a quote *means* its part (a paraphrase, a negation
    or hedge, "only", a comparison, an attribution) is the model's judgment. The negation,
    hedge and comparison word lists were retired after Gate A, so code no longer checks
    operators.
  - **Retired lexical gates.** The pre-ADR-0006 lexical gate refused any candidate whose
    capitalised names the Passage did not repeat; it is retired in production. The Writer's
    figure and name match is now retry feedback for current Claims (rules ledger row 17,
    2026-10-21): a Claim that still breaks it goes to the semantic check against its cited
    Passage and is published only if that check supports it.
  - **Limits.** Gate A, the one live measurement of this judgment, FAILED, and qualifier leaks
    (#75) remain open, so a SUPPORTED verdict is not a guarantee of entailment. Confidence
    reflects source count and credibility, not truth.
- **Fact-checking is a sample, by the same model.** It checks 20% of Claims (at least 5). A
  `verified` Claim had a candidate (its own cited Passage, or a span at or above the 0.85
  similarity threshold) containing the Claim's figures that the light model judged SUPPORTED
  with a support map that passed the code checks above. Only the Passage the Claim cites can
  verify it: support elsewhere in the Source leaves it unverified. Unsampled Claims are not
  checked by the Fact-Checker, and `[UNVERIFIED]` means only that the check found no support.
  Before publication every published Evidence item was judged against its cited Passages during
  Synthesis, every Claim that rewords its item against the item's primary Passage, and every
  summary sentence against the published Claims. These use the same contract and model as the
  Fact-Checker, so they are not an independent check.
- **Data handling.** Everything is stored locally: SQLite files (`*.db`, git-ignored), Qdrant,
  and the model cache. Fetched web pages and uploaded PDFs are kept for as long as a Report
  cites them. Other raw material is swept after `RETENTION_DAYS` (default 30). Reports are never
  deleted, and there is no delete endpoint. Brief text and Passage text are sent to the
  configured chat provider. Do not upload documents you cannot share with that provider.
- **Not production.** The app is one process on one node: no Postgres, no worker queue, no
  sign-up or credentials, no multi-process deployment. Search uses Tavily only (other listed providers are
  skipped). Hardening observations are tracked in issue #49 (T20).
- **Research quality on live models (seen in the live Runs above).**
  - Long official-documentation pages can exhaust the output limit. The Qdrant vs Weaviate Run
    left 5 extraction batches unread, so its recommendation was not established. The Report
    reports this as a gap and does not hide it.
  - Passages are single sentences, so a fact whose scope sits in a neighbouring sentence can
    lose that scope. Such a fact is now omitted when its reworded Claim is rejected, but
    Extraction still accepts it (verification record §15).
  - Repair still fills an emptied section or unanswered requirement with an item's statement,
    unchecked for context.
  - Section Confidence often shows LOW, and Source titles can carry page clutter.
  - Many Sources show as undated. The citation drawer shows the publication date only, not
    `updated_at`.
  - Cost and time are high. One Brief took 711 to 852 model calls and 43 to 97 minutes,
    mostly support judgments.
  - Qualifier leaks (#75) and the Stage C gaps (#76) remain open post-submission follow-ups.
- **Operational.**
  - There is no Run or Sub-task timeout.
  - PDF export depends on a running Next app and Chromium. It failed twice during the demo
    until the dev server was restarted.
  - The installed `qdrant-client` (1.19.1) warns against the pinned Docker server (1.12.4).
  - Integration tests and the real-PDF browser test skip in CI, which has no model cache.
