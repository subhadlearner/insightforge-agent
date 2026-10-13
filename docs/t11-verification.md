# T11 implementation and verification

Refs #12. Base: `main` at `ebe7c26dda1c99c56208fdc6b1f071a66b20e817`.
GitHub independently confirmed PR #51 merged at this commit before branching.

## Checkpoints

| Checkpoint | Implementation | Evidence |
|---|---|---|
| CP1 | Watchlist configuration/repositories, CRUD, Planner-only preflight, owner/source checks | `tests/api/test_t11_watchlist.py`, including revision-aware PATCH |
| CP2 | APScheduler interval jobs, durable due-time consumption, atomic Run admission, max_runs | `tests/stores/test_watchlist_repository.py` and `tests/api/test_t11_lifecycle.py` |
| CP3 | Dashboard counts and first-success initialization input with current Claim/Evidence provenance | `tests/stores/test_watchlist_baseline.py` and lifecycle tests |
| CP4 | Real scheduler/misfire, cancellation, two-connection races, recovery, clarification, post-processing failure | Final full offline suite: 1,560 passed |

## Executed results

- `uv run pytest -q`: **1,560 passed, 26 deselected** after the third review pass (previously 1,543). No test failures. Includes the focused T11/T10 shutdown tests and the existing T9/T10 regressions.
- `uv run pytest -q -m integration`: **19 passed, 1,567 deselected** after the third review pass, with local model caches present. (The earlier pass had all 19 skipped because the caches were absent.)
- `uv run lint-imports`: **4 kept, 0 broken**.
- `git diff --check`: clean.
- No live tests, paid model calls, real search calls, or model-weight downloads were performed.

An impacted-suite run encountered two environment-only failures because the environment's
`ALL_PROXY` took precedence for loopback HTTPX calls and the locked environment does not include
`socksio`. No project dependency was changed. Removing proxy variables for the offline test
process made both real-Uvicorn tests pass. Final commands used the same prefix:

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
  -u HTTPS_PROXY -u https_proxy -u FTP_PROXY -u ftp_proxy uv run pytest -q
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
- The second review pass is covered by transactional due-time merging, end-to-end tick revision
  admission, a shutdown guard inside admission, one-second early-fire tolerance, strict
  more-than-one-cadence misfire handling, final-run Baseline retry, broad non-temporary candidate
  quarantine, and schema-versioned quarantine re-evaluation. The service's shared early Brief
  predicate remains intentionally: it avoids a Planner call before the repository repeats the
  authoritative guard in its transaction.
- The third review pass: only candidate data defects quarantine a Report (store errors and
  `MemoryError` are retried); a tick that finds no usable source is skipped rather than raised
  to APScheduler; a temporary Baseline failure gets its own one-minute retry job instead of the
  research interval; a non-cadence edit keeps a tick due within the last cadence; ticks no
  longer re-read the Baseline; the dashboard reads recent Failed Runs with a bounded indexed
  query; and the quarantine `schema_version` migration lives in `_migrate`.
- **AC5 scope boundary:** T11 initializes durable provenance-rich current Claim/Evidence seeds,
  marked `normalization_pending=true`. This is the T11 persistence/initialization contract from
  the handover. A fully normalized, established per-Finding Baseline is **not implemented** here;
  T12 owns normalization, predicate cardinality/identity, polarity, temporal rules and Diff/Alert
  behavior. Seeds are never advertised as normalized Findings or used for Report-to-Report Diff.
- **T20 concern:** Watchlist writes and APScheduler's separate job-store writes are not one
  transaction. Startup repairs missing/stale jobs; a job-store write failure while the process
  stays up may require restart. Review alongside T9-1 in #49. The rest of #49 remains unchanged.
- The integration marker passed locally with caches present; real-provider behavior (live
  models and real search) is unverified. No merge recommendation is
  implied by scripted tests alone; issue #12 remains open for owner review.
