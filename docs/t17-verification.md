# T17 README verification

Refs #18. Base: `main` at `22dad4d07ed39741a8985cb06680040f6196b388` (PR #58 merge).
Date: 2026-10-18. No paid or hosted model, search or evaluation call was made.

## Fresh-clone record

A new clone of `https://github.com/subhadlearner/insightforge-agent.git` (branch `main`) in an
empty temporary directory, with no `.env` file. Machine: Windows 11 Enterprise, uv 0.12.5,
Python 3.12.14 (uv-managed), Node 24.20.0, npm 11.19.0, Docker CLI 29.8.2 (daemon not running).

| Step (README order) | Command | Result |
|---|---|---|
| Clone and install | `git clone ...`, `uv sync --locked` | exit 0 (17 s with a warm uv cache) |
| Import contracts | `uv run lint-imports` | 5 kept, 0 broken |
| Offline demo | `uv run insightforge-agent demo` | exit 0, every expectation PASS |
| Default suite | `uv run pytest` | 1,887 passed, 26 deselected, 2 warnings, 430 s |
| Model files | `uv run insightforge-agent models download` | exit 0: 384-dimension embedding, 1 Docling chunk |
| Integration | `uv run pytest -m integration` | 19 passed, 1,894 deselected |
| Web install | `npm install` (in `web/`) | exit 0, 0 vulnerabilities |
| Web unit and component | `npm test` | 10 files, 102 passed |
| TypeScript | `npm run typecheck` | exit 0 |
| Production build | `npm run build` | exit 0, 8 routes |
| Chromium | `npx playwright install chromium` | exit 0 |
| Browser E2E | `npm run test:e2e` | 14 passed (2.0 min) |
| eval CLI | `uv run insightforge-agent eval --help` | exit 0; flags as documented (not run: paid) |
| API startup | `serve --port 8765` with `SESSION_SECRET`, placeholder `GOOGLE_API_KEY`/`TAVILY_API_KEY`, `QDRANT_URL=:memory:` and scratch SQLite paths | started; `GET /personas` 200 (3 personas); `GET /reports` without cookie 401; `POST /session` 200 with an HttpOnly cookie; `GET /reports` 200 `[]`; `POST /research/estimate` 200 (75 s for web). No Run submitted |
| Startup refusal | `serve` without `SESSION_SECRET` | `ValueError: SESSION_SECRET is required to sign the persona cookie` |
| Startup refusal | `serve` with no search key | `Search provider 'tavily' skipped: no credentials`, then a `Settings` validation error |
| Startup refusal | `serve` with only a placeholder `SERPAPI_API_KEY` (added after PR review) | `Search provider 'serpapi' is not implemented yet; skipped`, then `SearchError: no implemented search provider is usable`, exit 1 (`Runtime.build` → `build_deps` → `agents/web.build_search`) |

### Caveats: what this record does not show

- **Caches were warm.** The uv package cache, the FastEmbed and Docling model cache and the
  Playwright Chromium install already existed on this machine. So `uv sync`, `models download`
  and `playwright install` did not prove a cold download. They did run as written and succeed.
  The integration result (19 passed) depends on that warm model cache. In CI, which has no model
  cache, the same step reports **19 skipped** (run 39064484395 on `22dad4d`).
- **Not executed: `docker compose up -d qdrant`.** The Docker daemon was not running, so the
  persistent Qdrant server path was not verified here. `QDRANT_URL=:memory:` was used instead.
- **Not executed: a real Run, a Watchlist tick, PDF export through a real API, `eval`, and
  `pytest -m live`.** All of these need a real provider or search key, and some of them cost
  money. The browser E2E suite covers the UI paths with scripted models.
- **Warnings.** The 2 default-suite warnings are an APScheduler `JobLookupError` raised in a
  scheduler thread during `tests/api/test_t11_lifecycle.py` teardown. The tests pass.
  Recorded here for T20 (#49); not changed by T17.
- Same commit, separate checkouts: the README changes themselves are documentation only, so
  these results apply to the T17 branch.

## Compaction evidence audit (criterion 2)

| Question | Finding | How it was checked |
|---|---|---|
| Is compaction middleware on the Planner? | Yes. `deepagents` 0.7.23 `create_deep_agent` always appends `create_summarization_middleware(model, backend)` to the main agent (and to each subagent) in `deepagents/graph.py`. `agents/planner.build_planner` does not exclude it | Read the installed library source |
| When does it trigger? | `compute_summarization_defaults`: 85% of the model profile's `max_input_tokens` if there is one, else 170,000 tokens (keep the last 6 messages). Offline, the configured Planner models give: `ChatAnthropic` `claude-haiku-5-5` no profile (170,000); `ChatGoogleGenerativeAI` `gemini-3.5-flash` 1,048,576 (fraction 0.85); `ChatGroq` `openai/gpt-oss-120b` 131,072 (fraction 0.85) | A scratch script (not committed) built each model with placeholder keys and called `compute_summarization_defaults`. No network call was made |
| Is a compaction recorded in the Run log? | No. The middleware keeps `_summarization_event` in the agent's private state. No InsightForge code reads it or writes to `run_events` | `grep -ri "compaction\|SummarizationMiddleware\|_summarization_event"` over `src/` and `tests/`: no matches |
| Is there a test? | No | Same grep |
| Has a real Run been compacted? | Not observed. No live-run record mentions it. Because nothing records it, the Run log would not show one | `docs/live-runs/` (T3, T4, T5) |

Conclusion: criterion 2 is **blocked**. Closing it needs production instrumentation and a test,
then evidence from a Run. That is outside the T17 documentation scope and is left to the owner.

## Issue #60 follow-up: compaction recorded (2026-10-18)

The audit above describes `main` before issue #60. Issue #60 added the recording:

| Question | Finding now | How it was checked |
|---|---|---|
| Is a compaction recorded in the Run log? | Yes. `agents/compaction.CompactionRecorder` (on the Planner and on every subagent) reports each completed compaction, and `pipeline/graph.record_compaction` appends one `context_compacted` event per `compaction_id` to `run_events` | `tests/pipeline/test_compaction_events.py` |
| How is the compaction made in the test? | By the real `SummarizationMiddleware`: the scripted models carry `profile={"max_input_tokens": 12000}`, so deepagents' own trigger is 85% of that, and the padded replies cross it. The middleware calls the model for the summary (the tests count those calls) and stores its event; the recorder reads it. No event is constructed by the tests | Same file; a mutation that removed the once-per-`compaction_id` check failed two of its tests |
| Is it durable and correctly attributed? | Yes, offline: read back from a reopened SQLite file; Planner events have `agent: planner` and no Sub-task; Researcher events name `researcher` and their own Sub-task; a restarted process on the same checkpoint records only new compactions | Same file |
| Do production thresholds change? | No. The ordinary fakes (no profile) record nothing | `test_without_a_small_context_window_nothing_is_compacted_or_recorded` |
| Has a real Run on a live model been compacted? | Not observed. No live Run was made (no paid calls) | — |

Persisted Run-log excerpt from the forced scenario (a scratch script, not committed, ran the
`test_each_later_compaction_is_recorded_and_none_twice` flow on SQLite files and read them back
on a new connection). It is reproduced in the README. Two Planner compactions are `seq=4`
(`cutoff_index` 4) and `seq=10` (`cutoff_index` 10, after the repair round), out of 6 Planner
model calls and 2 summary calls.

Compatibility risk: the recorder depends on deepagents' private state key
`_summarization_event` (read through the exported `SUMMARIZATION_EVENT_KEY`) and its
`SummarizationEvent` fields. It also depends on `create_deep_agent` placing caller middleware
after its summarization middleware. `tests/test_deepagents_facts.py` pins the first. The
integration tests catch the second.

## Reconciliations made visible in the README

- Design §4 said the Run log records each compaction. Not implemented at the T17 audit;
  implemented by issue #60 (above), and §4 now describes it.
- Design §4 said Evidence overflow is "compressed". The implementation cuts whole
  lowest-ranked items and logs each one as `evidence_cut` (T5 notes). Nothing is summarised;
  §4 now says so.
- `SEARCH_PROVIDERS` accepts `serpapi` and `brave`, but only Tavily is implemented. A SerpAPI
  or Brave key passes the `Settings` credential check, then `build_search` skips the provider
  and raises `SearchError` when no Tavily key is set, so the API does not start (T3 notes,
  `agents/web.py`). The first README draft got this wrong; corrected after PR review.
- CI runs the integration step without a model cache, so those tests skip there.
