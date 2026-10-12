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
uv run uvicorn insightforge_agent.api.app:app_from_env --factory
```

Runs execute in the API process on a bounded pool (`RUN_CONCURRENCY` workers, `RUN_QUEUE_LIMIT`
queued); a submission beyond that is refused with 503. Endpoints:

| Endpoint | What it does |
|---|---|
| `GET /personas`, `POST /session {"persona_id"}`, `GET` / `DELETE /session` | list and select a persona (sets the signed HttpOnly cookie) |
| `POST /research/run {"brief", "source_toggles"?, "documents"?}` | 202 with `run_id`; 422 if no source type is available; 503 at capacity |
| `GET /research/status?run_id=` | SSE progress; reconnect with `Last-Event-ID` to resume |
| `GET /research/{run_id}/clarification`, `POST /research/{run_id}/clarify {"answer"}` | the Run's clarifying question; 409 once resolved |
| `GET /reports`, `GET /reports/{report_id}` | the selected persona's Reports (there is no delete) |

Another persona's Run or Report is 404. Documents are sent as `{"filename", "content_base64"}`.

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
