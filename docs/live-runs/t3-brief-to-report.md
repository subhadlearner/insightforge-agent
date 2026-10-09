# T3 live run: Brief to Report (web only)

- **Date:** 2026-10-10
- **Commit:** the T3 branch after "use Anthropic text blocks" fix (see git log)
- **Provider:** Anthropic `claude-haiku-5-5` for every role, Tavily search, real page fetches. Gemini was tried first and hit its free-tier limit (15 requests/minute on `gemini-3.5-flash-lite`).
- **Command:** `LLM_PROVIDER=anthropic uv run pytest -m live tests/live -k brief -s`
- **Result:** 1 passed in 224 s. The Run reached `COMPLETE` and stored one Report.
- **Brief:** "Compare BYD and Tesla in battery electric vehicles: 2025 sales volume, pricing strategy and battery technology."
- **Report:** 3 sections (sales, pricing strategy, battery technology), 26 Claims, each citing an Evidence item. No Writer retries. The `gaps` list was empty. Run log showed 5 skipped pages (HTTP 403 or no readable body) and several `summary_input_limited` events for long pages.
- **Found by this run:** Anthropic replies carry thinking blocks, so reading `reply.content` as text broke JSON parsing (Extraction batches failed and the Writer exhausted retries). Fixed by reading `reply.text`, with a unit test.
