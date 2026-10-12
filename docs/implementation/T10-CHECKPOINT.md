# T10 checkpoint (branch-local handover; remove before merge)

Branch: `t10-api` (from `main` c205a36). Issue #11.

## Checkpoints

| CP | Scope | Status |
|---|---|---|
| CP1 | App composition root, persona session, owner-scoped Reports | done |
| CP2 | Bounded RunExecutor, durable Run creation and admission, `POST /research/run` | done |
| CP3 | SSE over run_events, clarification router, scheduler, startup sweep | next |
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

- CP2: `run_brief` = `check_brief` (validate, no writes) -> `start_run` (store Run) -> `carry_run`
  (graph), in `pipeline/graph.py`. `pipeline/executor.py` `BoundedRunExecutor(max_workers,
  max_queued)`: `admit` reserves capacity before `create`, so a full/closed executor creates no Run
  (`ExecutorFull`/`ExecutorClosed`, API 503 + Retry-After); a Run created as shutdown begins is
  settled FAILED (`NOT_STARTED`); `submit` (resumes) is never capacity-refused; `shutdown` cancels
  queued work (runs `on_cancel`) and waits for running work. `pipeline/research.py`
  `ResearchService.submit`. `api/research.py` `POST /research/run` (JSON; documents as base64;
  toggles default to the persona's). SQLite: every public repository method holds the shared
  RLock (`_Base.__init_subclass__`), since concurrent Runs share one connection.

## Planned (CP3-CP4)

- Resumes: a refused or cancelled resume leaves the Run RESUMING with a `resume_deferred` event,
  which the startup sweep resumes.
- `enter()` logs the stage event before setting the state, so every event of a Run is persisted
  before it becomes terminal and the SSE stream can drain and close exactly.
- Observation fix: `derive_observation_id` includes `run_id`; pipeline stages read a Source through
  `SourceStore.read_run_source(owner, run, source)` (the Run's own latest Observation).

## Tests

- Baseline on branch before changes: 1395 passed, 26 deselected; lint-imports 4 kept.
- CP1: `uv run pytest -q tests/api/test_t10_session_reports.py` -> 17 passed.
- CP2: `uv run pytest -q tests/api` -> 34 passed; full offline suite: all pre-existing tests
  passed (1413 passed incl. new); lint-imports 4 kept.

## Next step

CP3: reorder `enter()`; SSE `GET /research/status` in `api/research.py`; Runtime gets
scheduler (paused start), ClarificationService(submit=executor.submit), handler install,
startup_sweep, scheduler resume; shutdown order; tests `tests/api/test_t10_sse.py`,
`tests/api/test_t10_clarification_app.py`.
