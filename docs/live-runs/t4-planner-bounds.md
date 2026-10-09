# T4 live runs: Planner bounds and the dispatch guard

- **Date:** 2026-10-10
- **Command:** `uv run pytest -m live tests/live -k brief -s`
- **Provider and models:** Anthropic `claude-haiku-5-5` for planner, writer and light roles (from the local `.env`). Search: Tavily.
- **Brief:** "Compare BYD and Tesla in battery electric vehicles: 2025 sales volume, pricing strategy and battery technology."
- **Test assertions added for T4:** the final plan has 3 to 6 Sub-tasks, every Aspect is covered, only allowed source types are used (`plan_violations(plan, ["web"]) == []`), and the T4 events are printed (`dispatch_rejected`, `plan_correction`, `plan_trimmed`, `repair`, `subtask_failed`).
- **Not in the repo:** API keys and Source bodies.

## Three runs, one finding

All three runs passed (Run reached `COMPLETE`, one Report stored, plan assertions held). They differ in how the Planner dispatched research.

| Run | Code under test | `dispatch_rejected` | Other T4 events | Duration |
|---|---|---|---|---|
| 1 | guard, rejection reason only | 5 (s1 to s5) | none | 157 s |
| 2 | guard, rejection also logs the submitted description | 6 (s1 to s6) | none | 527 s |
| 3 | prompt requires the exact description | 0 | none | 91 s |

Plans: run 1 had 3 Aspects and 5 Sub-tasks (two Aspects with two Sub-tasks each); run 2 had 6 Sub-tasks; run 3 had 3 Aspects and a plan that passed the same assertions. All were web only, none needed a correction, trim or repair.

## What the refusals were (run 2)

Every refusal was "does not carry exactly its approved scope". The real Planner rewrote each approved query in its own words and appended boilerplate the Researcher does not need, for example:

> `SUBTASK_ID=s1. BYD 2025 full-year battery electric vehicle sales volume units. Source type: web. Time horizon: 12 months. Return findings with figures and sources, prefixed RESULT: on success or FAILED: on failure.`

So the guard did its job: scope drift was stopped before any Researcher ran. Nothing off-topic was dispatched. Each refusal cost the Planner a turn, and the Planner recovered by re-sending acceptable dispatches (no repair round was needed). The Researcher already knows the reply format, so the extra text was never needed.

## Fix and result

Prompt only. `PLANNER_PROMPT` and the RESEARCH and REPAIR messages now say the description must be exactly `SUBTASK_ID=<id> <query>` with the query copied verbatim and nothing added. The guard is unchanged. Run 3 had no refusals and ran in 91 s against 527 s.

## Limits of this evidence

- One run per code version. Live Planner output varies, so zero refusals in run 3 is encouraging, not a guarantee. If refusals return, `dispatch_rejected` now carries the submitted description for diagnosis.
- Run 1's refusal payloads did not include the description, so its cause is inferred from run 2.

## Not part of T4

The live run reviewed on the PR also showed `extraction_batch_failed` (1), `evidence_dropped` (16) and `evidence_cut` (51). These belong to extraction and synthesis (T5) and are left there.
