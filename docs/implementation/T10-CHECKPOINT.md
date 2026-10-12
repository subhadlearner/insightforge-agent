# T10 checkpoint (branch-local handover; remove before merge)

Branch: `t10-api` (from `main` c205a36). Issue #11.

## Checkpoints

| CP | Scope | Status |
|---|---|---|
| CP1 | App composition root, persona session, owner-scoped Reports | done |
| CP2 | Bounded RunExecutor, durable Run creation and admission, `POST /research/run` | next |
| CP3 | SSE over run_events, clarification router, scheduler, startup sweep | todo |
| CP4 | Cross-Run Observation fix, regressions, full verification, PR | todo |

## Decisions so far

- Persona cookie `insightforge_persona` = `b64(persona_id).b64(HMAC-SHA256(SESSION_SECRET, purpose||id))`,
  HttpOnly, SameSite=Lax, Path=/, Secure from `SESSION_COOKIE_SECURE`. Stdlib `hmac`, no new
  dependency. `PersonaSession.current_user()` is the one dependency; missing, malformed, tampered,
  or unknown-persona cookies are 401. `SESSION_SECRET` (>= 32 chars) is required by `Runtime.build`.
- Endpoints: `GET /personas`, `POST /session {persona_id}` (unknown -> 422), `GET /session`,
  `DELETE /session`; `GET /reports` (newest first), `GET /reports/{id}` (foreign = unknown = 404).
  No Report delete route.
- Composition root: `api/app.py` `Runtime.build` / `start` / `stop`, `create_app(runtime)`,
  `app_from_env` uvicorn factory.

## Planned (CP2-CP4)

- Split `run_brief` into `check_brief` (validate, no writes) -> `start_run` (create Run) ->
  `carry_run` (graph). `ResearchService` admits through `BoundedRunExecutor.admit`: capacity is
  reserved before the Run is created, so a full executor is 503 with no Run; a Run created and then
  refused because the executor closed is settled FAILED. Resumes use `submit` (never capacity
  refused); a refused or cancelled resume leaves the Run RESUMING with a `resume_deferred` event,
  which the startup sweep resumes.
- SQLite: one shared connection is used by concurrent Runs, so every repository method is
  serialised by the shared RLock.
- `enter()` logs the stage event before setting the state, so every event of a Run is persisted
  before it becomes terminal and the SSE stream can drain and close exactly.
- Observation fix: `derive_observation_id` includes `run_id`; pipeline stages read a Source through
  `SourceStore.read_run_source(owner, run, source)` (the Run's own latest Observation).

## Tests

- CP1: `uv run pytest -q tests/api/test_t10_session_reports.py` -> 17 passed. `lint-imports` 4 kept.

## Next step

CP2: `pipeline/executor.py`, refactor `run_brief`, `pipeline/research.py`, `api/research.py`
(`POST /research/run`), tests in `tests/api/test_t10_research.py`.
