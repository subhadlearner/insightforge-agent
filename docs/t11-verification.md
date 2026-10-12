# T11 implementation and verification

Refs #12. Base: `main` at `ebe7c26dda1c99c56208fdc6b1f071a66b20e817`.
GitHub independently confirmed PR #51 merged at this commit before branching.

## Checkpoints

| Checkpoint | Implementation | Evidence |
|---|---|---|
| CP1 | Watchlist configuration/repositories, CRUD, Planner-only preflight, owner/source checks | `tests/api/test_t11_watchlist.py`, including revision-aware PATCH |
| CP2 | APScheduler interval jobs, durable due-time consumption, atomic Run admission, max_runs | `tests/stores/test_watchlist_repository.py` and `tests/api/test_t11_lifecycle.py` |
| CP3 | Dashboard counts and first-success initialization input with current Claim/Evidence provenance | `tests/stores/test_watchlist_baseline.py` and lifecycle tests |
| CP4 | Real scheduler/misfire, cancellation, two-connection races, recovery, clarification, post-processing failure | Final full offline suite: 1,534 passed |

## Executed results

- `uv run pytest -q`: **1,534 passed, 26 deselected**, 75.34 seconds. Two Qdrant compatibility-check warnings; no test failures. Includes **65 focused T11 tests** and the existing T9/T10 regressions, including both real-Uvicorn shutdown tests on Linux.
- `uv run pytest -q -m integration`: **19 skipped, 1,541 deselected**, 8.01 seconds. All 19 tests skipped because the required local model caches were absent; these tests are **not verified** here. Two Docling deprecation warnings.
- `uv run lint-imports`: **4 kept, 0 broken**.
- `git diff --check`: clean.
- No live tests, paid model calls, real search calls, or model-weight downloads were performed.

The first full run encountered seven environment-only failures because the environment sets a
SOCKS proxy but the locked environment did not have `socksio`. Installed `socksio==1.0.0` only
in the local venv (no dependency or lockfile change), and set `NO_PROXY`/`no_proxy` to
`127.0.0.1,localhost` for loopback subprocess tests. Final command:

```bash
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost uv run pytest -q
```

The test suite's existing offline network guard remained enabled. Model-client construction
checks use fake keys and do not invoke the models.

For the remaining integration verification, run this on a checkout with the project's local
Docling and fastembed model caches already populated:

```bash
uv run pytest -q -m integration
```

## Review conclusions and boundaries

- Both SQLite connections and executor admission enforce ownership, revision, overlap and limit
  invariants. Run rows are the durable counter. No counter is incremented for capacity refusal.
- Startup repairs abandoned Runs before resuming Watchlist schedules; missed ticks are skipped.
- Deletion preserves admitted Runs and Reports. Post-processing cannot recreate deleted items.
- Draft-PR review follow-up fixed cadence-wide dispatch grace, corrupt-candidate quarantine,
  due-time preservation for non-cadence edits, revision-aware PATCH, lock-free stop signaling,
  indexed Baseline candidate lookup, shared Run insertion and the shared Brief-change guard.
- **AC5 scope boundary:** T11 initializes durable provenance-rich current Claim/Evidence seeds,
  marked `normalization_pending=true`. This is the T11 persistence/initialization contract from
  the handover. A fully normalized, established per-Finding Baseline is **not implemented** here;
  T12 owns normalization, predicate cardinality/identity, polarity, temporal rules and Diff/Alert
  behavior. Seeds are never advertised as normalized Findings or used for Report-to-Report Diff.
- **T20 concern:** Watchlist writes and APScheduler's separate job-store writes are not one
  transaction. Startup repairs missing/stale jobs; a job-store write failure while the process
  stays up may require restart. Review alongside T9-1 in #49. The rest of #49 remains unchanged.
- Integration caches and real-provider behavior are unverified. No merge recommendation is
  implied by scripted tests alone; issue #12 remains open for owner review.
