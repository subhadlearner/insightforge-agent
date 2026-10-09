# T3 live run: Brief to Report (web only)

- **Date:** 2026-10-10
- **Command:** `LLM_PROVIDER=anthropic uv run pytest -m live tests/live -k brief -s`
- **Result:** 1 passed in 165 s.
- **Provider and models:** Anthropic `claude-haiku-5-5` for planner, writer and light roles. Search: Tavily. Pages fetched over HTTP. Gemini was tried first and hit its free-tier limit (15 requests/minute on `gemini-3.5-flash-lite`).
- **Run ID:** `run_82583c216045`
- **Report ID:** `rep_5987cd452552`
- **Final state:** `COMPLETE`, with exactly one Report stored for the User (the test asserts it).
- **Stage progression:** PLANNING, RESEARCHING, EXTRACTING, SYNTHESIZING, WRITING, FACT_CHECKING, COMPLETE (INGESTING skipped).
- **Brief:** "Compare BYD and Tesla in battery electric vehicles: 2025 sales volume, pricing strategy and battery technology."
- **Not in the repo:** API keys and Source bodies. The test prints only IDs, stages and the Report's Claims.

## What earlier live attempts found

1. Anthropic replies carry thinking blocks, so reading `reply.content` as text broke JSON parsing. Fixed by reading `reply.text`.
2. The Writer's JSON reply was cut off mid-string at the 4096-token default for `ChatAnthropic`, so a long Report failed its first attempt (and, once, all three). Fixed by giving the Anthropic writer `max_tokens=16000`.
3. The test's first reads of the Run log used `read_after`'s default limit of 500 events, which hid the later stages. It now reads the whole log.
