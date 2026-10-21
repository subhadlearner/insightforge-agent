# Backend tests

Run from the repository root: `uv run pytest` (pytest's `testpaths` points here). Outbound
network is blocked for every test (`conftest.py`, `network_guard.py`); loopback is allowed.

| Folder | What it covers |
|---|---|
| `domain/` | Pure rules: plan bounds, periods, quantities, support contract, Findings, Diffs |
| `pipeline/` | The LangGraph stages with scripted models: research, extraction, synthesis, writing, fact-checking, memory, documents, clarification |
| `stores/` | SQLite and Qdrant repositories, Watchlist and Diff lifecycle, retention |
| `api/` | The FastAPI app through its routes, including SSE, export, shares and the scheduler |
| `evaluation/`, `benchmark/` | The `eval` harness and the offline Evidence-quality benchmark |
| `integration/` | Real Docling, FastEmbed and in-memory Qdrant, offline from the local model cache (`-m integration`) |
| `live/` | Real model or search calls (`-m live`); also needs `INSIGHTFORGE_ALLOW_LIVE_TESTS=1`, and some files need explicit owner authorisation |
| `web_e2e/` | The scripted backend the browser E2E suite in `src/frontend/` starts |
| `fixtures/` | Recorded pages, gold sets and saved model replies; kept byte for byte |

Tiers (`docs/benchmark/methodology.md`): `baseline`, `replay`, `scripted`, then everything
else; `live` and `integration` are excluded by default and selected with `-m`.

## Historical tests

Files named `test_historical_*` and the helpers `t18_acceptance.py`, `t18_stage_b_support.py`
and `research_integrity_gate.py` underpin the published T18 and Gate A results
(`docs/t18-verification.md`, `docs/audits/2026-10-21-research-redesign-verification.md`).

- The offline rule tests (`test_historical_acceptance_rules.py`, `test_historical_stage_b_cases.py`,
  `test_historical_stage_b_harness.py`, `test_support_gate_decision.py`) run in the default suite.
- The `integration/test_historical_*` replays need the saved T18 evaluation, which is kept
  outside the repository: set `T18_EVAL_DB` (or `T18_EVAL_DIR`). Without it they **skip**; a
  skip is not a pass.
- `live/test_historical_stage_b_live.py` and `live/test_support_gate_live.py` make paid calls
  and run only with the live opt-in and the owner's authorisation. They are never run in CI.

Test modules were renamed on 2026-10-22; `docs/README.md` maps the old names.
