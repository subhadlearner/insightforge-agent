# F3: unparseable light-model JSON replies (2026-10-12)

**Scope:** audit finding F3 (`2026-10-11-evidence-quality-audit.md`). Branch `f3-json-reply-diagnostics` from `61e66b9`. No paid or live call was made. No model configuration, prompt, batching or retry behaviour was changed. T8 and F5-F7 are out of scope.

**Result:** truncation is **still unproven**. Nothing that survives can tell truncation from malformed JSON or a one-field schema error. The diagnostics are now in place to tell them apart. A one-call live probe is proposed in section 6 and has **not** been run.

## 1. Historical facts

| | |
|---|---|
| Run | 2026-10-10 T5 live run, code `7f790d8` (`docs/live-runs/t5-extraction-synthesis.md`) |
| Models | Anthropic `claude-haiku-5-5` for every role |
| Failures | 5 batches, each skipped and logged, no retry: 1 `extraction_batch_failed` (Source `src_c608a15bb5e4e465`), 4 `draft_batch_failed` (Sub-tasks `s5`, `s4`, `s4`, `s6`) |
| Logged per failure | `"<schema>: <n> problem(s) in <repr of the first 200 characters>"` |
| Provider tokens | Whole Run, all roles together: 465,652 total, 106,214 output, of which 34,315 reasoning. Nothing per call or per role. |
| Light-role ceiling then | Same as now (section 3): `llm.py` set no `max_tokens` for `light`, and `langchain-anthropic` is the same locked version, so the request carried `max_tokens=4096` |
| Drafting prompt then | `DRAFT_SYSTEM` is byte-for-byte unchanged since `7f790d8`. The Extraction `SYSTEM` prompt has changed since (T7 wording). |

Two related records were also found:
- **T4 live run (2026-10-10, `docs/live-runs/t4-planner-bounds.md`).** One more `extraction_batch_failed`. That run printed only event types, so nothing about the reply survives.
- **Uncommitted T3 development run (2026-10-09 18:52 UTC, before `e4a62f8`).** That code parsed the raw content list rather than `reply.text`. A light-model Extraction reply failed because its content began with a thinking block. This shows that, with no thinking setting, `claude-haiku-5-5` on the light role does produce reasoning. It says nothing about the five T5 failures.

## 2. What evidence survives

Searched read-only, printing no keys, Source text or reply text:
- **SQLite.** None. The live runs used `MemoryRepos` and an in-memory checkpointer, and no `insightforge*.db` exists in the repo, the home directory or temp.
- **Repo.** No untracked or ignored log or output files.
- **The T5 session's scratch directory.**
  - It still holds the scratch script `live_t5.py` and its output `live_t5_raw.txt`.
  - The script printed each event payload cut to 400 characters. It captured usage only as a whole-Run total (`get_usage_metadata_callback`).
  - The five `detail` strings in it are the logged ones (each payload is under 400 characters, so nothing was cut). **Nothing beyond the first 200 characters of any reply survives.**
- **Session transcripts.** They contain the same printed lines and nothing more.

Structural reading of the five surviving heads (lengths, nesting and value types only):

| Batch | Problems | Head | Shape up to character 200 | Value types complete inside the head |
|---|---|---|---|---|
| extraction | 1 | exactly 200 chars | well-formed `{"facts": [{...`, cut inside the first fact | all as the schema expects |
| draft `s5` | 1 | exactly 200 chars | well-formed `{"items": [{...`, cut inside the first item | all as expected |
| draft `s4` | 1 | exactly 200 chars | the same | all as expected |
| draft `s4` | 1 | exactly 200 chars | the same | all as expected |
| draft `s6` | 1 | exactly 200 chars | the same | all as expected |

So every reply was at least 200 characters long after the code fence was stripped. Each began as valid JSON of the right shape, and the single problem lies somewhere after character 200.

## 3. What cannot be known retrospectively

- Each reply's length, its ending, the provider's stop reason, and its output or reasoning tokens.
- Which kind of failure the one problem was. Pydantic reports **any** JSON syntax error, an early end included, as exactly one `json_invalid` error (checked offline). A single schema error also gives exactly one problem, for example a `null` or a bare number in a string field such as `value`, `entity` or `as_of_period`. So "1 problem(s)" fits all of these:
  - a cut-off reply;
  - malformed JSON such as a bad escape or trailing prose;
  - a one-field schema error.
- The inputs. Source bodies were never stored, so the failing batches cannot be replayed.

The 4,096 ceiling, the 34,315 reasoning tokens and the 200+ character replies are consistent with truncation but do not establish it. Per the brief, reply length alone is not used to infer it.

## 4. Current model configuration (checked offline)

Installed versions: `langchain-anthropic` 1.7.5, `langchain-core` 1.6.7, `anthropic` 1.12.1. Each `build_chat_model(role)` was built with a dummy key, and the request payload was inspected without sending it.

| Role | `max_tokens` | Thinking | Where the value comes from |
|---|---|---|---|
| `light` | **4096** (also the value in the request payload) | not set (model default) | `ChatAnthropic.set_default_max_tokens`: the model profile's `max_output_tokens`, else `_FALLBACK_MAX_OUTPUT_TOKENS = 4096`. No profile value applies to `claude-haiku-5-5`. |
| `writer` | 16000 | not set | `llm.py`, set after the T3 Writer reply was cut off at the default |
| `planner` | 4096 | disabled | `llm.py` (signed thinking blocks break resumed threads) |

Where the installed integration puts the metadata. This was checked by driving the real `ChatAnthropic.invoke` with the HTTP call replaced by an `anthropic.types.Message`. The same check is pinned in `tests/test_llm_json.py`.

| Field | Location |
|---|---|
| Stop reason | `AIMessage.response_metadata["stop_reason"]` (`"end_turn"`, `"max_tokens"`, ...). `stop_sequence`, `stop_details` and the raw `usage` sit alongside it. |
| Output tokens | `AIMessage.usage_metadata["output_tokens"]` |
| Reasoning tokens | `AIMessage.usage_metadata["output_token_details"]["reasoning"]`, from Anthropic's `usage.output_tokens_details.thinking_tokens`. They are part of `output_tokens`, not added to it, so they share the `max_tokens` budget. |
| Thinking blocks | `AIMessage.content` becomes a list. `AIMessage.text` joins only the text blocks, which is what `ask_json` parses. A reply with only thinking gives empty text. |
| Gemini, Groq | `response_metadata["finish_reason"]` (merged from `generation_info` by `langchain-core`) |

## 5. Diagnostic improvement (`agents/llm_json.py`)

`BadModelReply` now carries a frozen `ReplyDiagnostic` (`.diagnostic`). Its `str()` is the rendered, bounded diagnostic that Extraction, Synthesis and the Fact-Checker already log. The callers are unchanged.

| Field | Content |
|---|---|
| `schema` | the Pydantic model's name |
| `kind` | `empty`; `incomplete_json` (Pydantic's "EOF while parsing": the JSON stopped before it closed); `invalid_json` (any other syntax error); `schema` (valid JSON that fails the model) |
| `error_count`, `error_types`, `error` | the count, the distinct Pydantic error types (at most 5), and the first message (at most 120 characters, without the input) |
| `length` | characters of reply text |
| `head`, `tail` | the first 200 characters, and the last part after the head (at most 200; empty for a reply of 200 characters or fewer, so nothing is shown twice) |
| `stop_reason` | `response_metadata` `stop_reason`, else `finish_reason`; else "not reported" |
| `output_tokens`, `reasoning_tokens` | from `usage_metadata`, where present |
| `max_tokens` | the model's configured ceiling, if it exposes one |

Example rendered line (under 1,000 characters for ordinary text; escapes in the head and tail can lengthen it):
`_Drafts: incomplete_json, 1 problem(s) [json_invalid: Invalid JSON: EOF while parsing a string at line 1 column 9876]; stop_reason=max_tokens; output_tokens=4096 (reasoning 3100); max_tokens=4096; 9876 chars; head='...'; tail='...'`

- **`incomplete_json` is a syntactic finding only.** Truncation is shown by `stop_reason=max_tokens`, or `MAX_TOKENS` / `length` from other providers.
- **Bounded.** The full reply is never kept.
  - The exception is raised outside the `except` block, so no chained `ValidationError` (which holds the input) is reachable from it.
  - The reply is dropped from `ask_json`'s locals before the raise, so the traceback's frame does not keep it either.
  - Tests check `__cause__`, `__context__` and the frame's locals.
- **Provider-neutral.** `provider_report(message)` reads only `response_metadata` and `usage_metadata`; the ceiling comes from `getattr(model, "max_tokens")`. No provider import. The live probe reuses `provider_report` and `reply_ends`, so it reads the metadata exactly as the diagnostic does.
- **Pydantic dependency.** `incomplete_json` relies on pydantic-core's "EOF while parsing" wording. The tests pin it, so a wording change fails them rather than silently reclassifying.
- **Known edge.** A reply cut inside its closing code fence is classified `invalid_json`, because the partial fence is not stripped. The stop reason still shows the cut.
- **Writer.** It feeds the error back to the model on a retry, so it now uses `e.brief`, the old message byte for byte. Its prompt is unchanged (test: `test_writer_retry_prompt_keeps_the_short_bad_json_message`).
- **Fact-Checker.** It logs `f"{type(e).__name__}: {e}"[:300]`. The classification, error, stop reason and tokens come first in the rendered line, so in the usual case that cut keeps them and drops only the head and tail. The Fact-Checker was not changed, so its log lines are the one place where the head and tail are usually lost. It is outside the five F3 failures.
- Successful replies and fenced JSON parse exactly as before. No retry was added.

Tests: `tests/test_llm_json.py`, which includes the installed `ChatAnthropic` driven offline, and `tests/benchmark/test_scripted_tier.py`, which checks the Draft and Extraction failure events.

## 6. Decision gate and proposed live probe (not run)

The historical evidence is **inconclusive**. Truncation is neither proven nor disproven, so no configuration change is proposed yet.

**Proposed probe:** `tests/live/test_f3_json_probe.py`.
- **Calls:** exactly **one** paid `claude-haiku-5-5` call on the current light configuration (`build_chat_model("light")`, `max_tokens=4096`). The one change is that the Anthropic SDK's own automatic retries (default 2, on 429, 5xx and timeouts) are turned off (`max_retries=0`), so one call is one request. An offline test checks this on the SDK client itself.
- **What it runs:** the drafting call exactly as `_draft` makes it. That is `DRAFT_SYSTEM` (unchanged since the failing run), the `Sub-task: ...` message over `render_batch`, and `ask_json(..., _Drafts)`.
- **Its batch:** one full batch from `batch_passages` at the production cap. That is 8 Passages and 96 distinct figures about fictional companies, about 2,750 estimated tokens.
- **Excluded:** web search, Planner, Researcher, Writer and Report.
- **What it records:** the stop reason, output and reasoning tokens, reply length, whether it parsed (item count, or the new diagnostic), and a bounded head and tail.
- **Retries and assertions:** none, whatever the result. It asserts only its own set-up and the call count.
- **How it runs:** it needs `INSIGHTFORGE_ALLOW_LIVE_TESTS=1` and `-m live`.
- **Scope:** about 3,000 input tokens and at most 4,096 output tokens.
- **Why drafting:** it accounts for 4 of the 5 failures, and its prompt is the one that failed. The Extraction prompt has changed since.

The batch is deliberately a **worst case for reply length**, because the real inputs are lost. That makes the result one-sided:
- **`stop_reason=max_tokens`** shows that a full drafting batch on the current light ceiling *can* be cut off. That is the mechanism the hypothesis needs, though it does not prove that each of the five historical replies was cut.
- **`end_turn` well under 4,096 output tokens** on this worst case is strong evidence against truncation at the current cap.
- **`end_turn` close to the ceiling** is inconclusive. It would be reported as such and not retried.

**Zero-cost alternative.** The next live Run the owner authorises anyway records the stop reason and tokens for any failed batch, on real pages.

**Remediation, not pre-approved.** If truncation is demonstrated, the smallest remedy is an explicit `max_tokens` for the Anthropic light role in `llm.py`. 16,000, as for the Writer, is the obvious candidate. The value is the owner's decision on the evidence. Thinking, prompts, retries, batching and Gemini/Groq settings stay as they are.
