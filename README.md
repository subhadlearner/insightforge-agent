# InsightForge

An autonomous research platform: a user submits a research question and receives a cited
intelligence report. See `docs/design.md` for the design and `CONTEXT.md` for the vocabulary.
(The full README is T17.)

> **The persona picker is identity selection for the demo. It is not authentication and must
> not be treated as a security boundary.** Anyone who can reach the API can select any persona
> in `users.yml`. The session cookie is signed (HMAC with `SESSION_SECRET`) and HttpOnly only so
> that a browser cannot switch persona by editing it; there are no passwords and no accounts.

## Running the API (T10)

Set the provider and search keys in `.env` (see `.env.example`), plus `SESSION_SECRET` (at least
32 characters; the app refuses to start without it). Start Qdrant and the API:

```
docker compose up -d qdrant
uv run insightforge-agent serve --port 8000
```

Stop it with Ctrl+C: open progress streams end at once (clients reconnect with
`Last-Event-ID`), the scheduler and executor stop, running Runs finish, and the databases are
closed. Plain `uvicorn insightforge_agent.api.app:app_from_env --factory` serves the same app,
but on shutdown uvicorn then waits for open streams for up to `SSE_MAX_STREAM_SECONDS` (60 s).

Runs execute in the API process on a bounded pool (`RUN_CONCURRENCY` workers, `RUN_QUEUE_LIMIT`
queued); a submission beyond that is refused with 503. Endpoints:

| Endpoint | What it does |
|---|---|
| `GET /personas`, `POST /session {"persona_id"}`, `GET` / `DELETE /session` | list and select a persona (sets the signed HttpOnly cookie) |
| `POST /research/run {"brief", "source_toggles"?, "documents"?}` | 202 with `run_id`; 422 if no source type is available; 503 at capacity |
| `POST /research/estimate {"source_toggles"?, "document_count"?}` | a heuristic runtime estimate in seconds, shown before submit; creates nothing |
| `GET /research/status?run_id=` | SSE progress; reconnect with `Last-Event-ID` to resume |
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

## Web UI (T14)

The Next.js app in `web/` is the research workbench: pick a persona, write a Brief, choose
sources and attach PDFs, see the runtime estimate, follow the Run live, answer a clarifying
question, and read the Report with a citation drawer for every Claim. Run the API as above,
then in a second terminal (Node 20.9 or later):

```
cd web
npm install
npm run dev
```

Open http://localhost:3000 (use `localhost`: `next dev` blocks its development assets for
other host names such as `127.0.0.1`). The browser only talks to Next; `/api/*` is rewritten
to FastAPI, so the persona cookie and the live progress stream are same-origin and no CORS is
configured. `INSIGHTFORGE_API_ORIGIN` (default `http://127.0.0.1:8000`) tells the Next server
where FastAPI is; `next dev` reads it at start, and `next build` bakes it into the build that
`next start` serves.

| Command (in `web/`) | What it does |
|---|---|
| `npm test` | unit and component tests (Vitest, jsdom) |
| `npm run typecheck` | TypeScript |
| `npm run build` | production build |
| `npm run test:e2e` | Playwright through the real rewrite, against `tests/web_e2e/server.py` (scripted models, no keys); first run `npx playwright install chromium`; needs `uv` |
| `npm run api:types` | regenerate `lib/api-schema.d.ts` from a running API's OpenAPI schema |

## Offline demo: Extraction and synthesis

Exercise the real Extraction and synthesis stages with seeded Sources and scripted models. No
API keys, no network, no extra dependencies:

```
uv run insightforge-agent demo
```

It prints the extracted facts and conflicts, the ranked Evidence with its supporting Sources,
Confidence labels and decision reasons, corroboration across number formats ("4.27 million" and
"4,270,000"), a contradiction between two Sources, and the candidates the pipeline rejects. It
ends with a PASS/FAIL list and exits non-zero if any expectation fails.

Only the model is scripted (`src/insightforge_agent/demo.py`); every decision shown is made by
the production pipeline.

## Uploaded documents (T7)

PDF uploads are chunked by Docling and searched semantically in Qdrant with a local embedding
model (`BAAI/bge-small-en-v1.5` via fastembed). No API key is needed for either. Fetch the model
files once, then start Qdrant:

```
uv run insightforge-agent models download
docker compose up -d qdrant
```

`QDRANT_URL` defaults to `http://localhost:6335` (`:memory:` runs Qdrant in-process). The sample
document brief is `samples/briefs/document-annual-report.yml`. Integration tests with the real
models run offline from the local cache: `uv run pytest -m integration`.

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

T11 persists current Claim/Evidence seeds with full provenance on first success, marked
`normalization_pending=true`. Normalized Finding state, comparisons, Material changes and
Alerts remain T12; the Baseline is never treated as just a previous Report pointer.
