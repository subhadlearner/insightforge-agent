# Laya: API, licence and data-handling investigation (M1.1)

- **Ticket:** #29 (spike #26). **Verified:** 2026-10-10. No calls were made to Laya; this is a read of public pages only.
- **Substitution.** #29 asked for the same investigation of Jev (TypeSafe AI, hosted). The owner chose **Laya** (Convai Innovations, open source, self-hosted) instead. Jev stays in the spike's vocabulary only as the reference API Laya imitates. Where a Jev-shaped question (pricing, account retention) has a different answer for a self-hosted model, the answer is stated for Laya.
- **Limits of this read.** The PyPI, GitHub and model-card pages were summarised by a fetch tool; the PyPI page and GitHub README were truncated (the last ~40k and ~31k characters unread). The repo `LICENSE`, `SECURITY.md`, `BENCHMARKS.md` and the documentation site were not opened. Items marked **unverified** need a direct read before M2.

## Sources

| Tag | Source |
|---|---|
| [HF] | https://huggingface.co/convaiinnovations/laya (model card) |
| [PyPI] | https://pypi.org/project/laya/ (version 0.4.2 at verification) |
| [GH] | https://github.com/NandhaKishorM/laya |
| [Docs] | https://nandhakishorm.github.io/laya/ (not read) |

## What it is

A non-autoregressive "System 1" model: given a state (text, email, ticket or JSON) and typed questions, it returns answers with probabilities in one forward pass. "It never generates text, so there is nothing to parse and nothing to hallucinate." [HF] Three checkpoints: `laya` (English, ModernBERT-large, ~421M parameters, ~808 MB), `laya-multilingual` (mmBERT-base, ~322M, ~647 MB), `laya-typed-decisions` (fine-tuned for typed decisions). A `Router` chooses a checkpoint per request. [HF][GH]

## What the outputs can express

| Type | Output | Meaning |
|---|---|---|
| `choice` | `answers[q]["choice"]` | One label picked from a `criteria` dict |
| `score` | `answers[q]["score"]` | A position on an ordinal `criteria` list |
| `noul` | `answers[q]["noul"]` | P(true) in `[false, true]` slot order; optional `labels` change only the text shown to the model |

- **No quoted spans and no free-form explanation.** The only locator is `answer["window"]` (index, token span, count of the window that decided a long input, via `predict_long`) [GH]. That is a window, not a verbatim span. Span grounding therefore cannot be asked of Laya; it stays a code check (does the span occur in the Passage) or a task for the LLM extraction call (candidate 2).
- Probabilities are **not Evidence Confidence** (ADR-0005 rule 4). Base checkpoints ship **over-confident**; refitting one temperature per (question type, option count) cut mean ECE from 0.466 to 0.081 (`laya`) and 0.314 to 0.106 (`laya-multilingual`). The card says to do this on our own data before trusting probabilities. [HF]
- Known weak spots relevant to us [HF]:
  - Zero-shot typed-decisions benchmark is near chance for the base checkpoints (0.362 English, 0.352 multilingual); 0.766 is the **fine-tuned** checkpoint.
  - `score` is the weakest primitive (SST-5 0.372).
  - `noul` can follow its `false:`/`true:` labels over the state; the card suggests a two-option `choice` instead (upstream issue #156).
  - `action.act_probability` "carries no usable signal yet" (issue #185). We would not use it.
  - High-cardinality choices degrade (Banking77, 77 labels: 0.425). Our questions have two to a few options, so this should not bite.
- The card recommends fine-tuning on our own domain for accuracy. Fine-tuning is out of scope for M1 and would need labelled data beyond the benchmark's held-out cases.

## Input size, latency, cost

| Item | Value | Source |
|---|---|---|
| Context, `laya` (English) | 512 tokens (`head_max_len` 192) | [HF] |
| Context, `laya-typed-decisions` | 1,024 tokens | [HF] |
| Context, `laya-multilingual` | 1,024 default; 8,192 with `max_len=8192` (default "cuts long documents off") | [HF][GH] |
| Long-document accuracy | 16 to 18 of 20 up to ~4,000 tokens, then 8 to 17 of 20 | [HF] |
| Server limits | 100 options per `choice`; `/v1/systemone/batch` up to 64 states | [GH] |
| Latency, T4 GPU, 1 question | 39.5 ms English, 32.8 ms multilingual | [HF] |
| Latency, T4, 10 questions batched | 158.6 ms English, 72.3 ms multilingual | [HF] |
| Latency, CPU, preloaded | 193 to 464 ms per question | [HF] |
| Checkpoint reload | 7.4 s (CPU), 10.3 s (T4) | [HF] |
| Price | **None per call.** Self-hosted; cost is our compute. No hosted Laya offering found (a demo Space and a donation link exist). | [HF][GH] |
| Reasoning tokens, caching | Not applicable: no generation. | [HF] |
| Runtime footprint | Python 3.10+, PyTorch and Transformers; extras `laya[serve]`, `[onnx]` etc. | [PyPI] |

Implication: the **512-token English limit is the binding constraint**. A SUPPORT or PAIR case that carries a Passage plus a statement will often exceed it; the multilingual checkpoint at `max_len=8192`, windowing via `predict_long`, or pre-trimming the Passage to the relevant sentence are the options. M1 should measure truncation as its own failure class. Latencies above are the authors' figures; the card's Jev comparisons are third-party and were not measured by them.

## Retention and data handling

- **Self-hosted inference runs locally**, so Source text and Passage text do not leave our machine when we run the library or `laya-serve`. There is no account, so there are no "account" retention terms. [HF]
- Checkpoints download from the Hugging Face Hub on first use. [HF] Normal Hub access applies (the download request, not our data).
- **Training statement.** The card names no training dataset (it describes only the RLCD training method). Nothing is said about training on user data, which is moot for local inference. **Unverified:** `BENCHMARKS.md`, the docs site and repo for any telemetry. The README excerpt read contains no telemetry or data-collection statement.
- **The demo Space** is hosted by Convai; the card says nothing about its retention. **Do not send benchmark cases to it.**
- **`laya-serve` security.** It binds `0.0.0.0` and has **no authentication unless `LAYA_API_KEY` is set**, then requires a bearer token (401 on malformed, 422 on malformed questions). Hook fields over HTTP are refused with 422. [HF][GH] If we ever run it, bind to localhost and set the key. Not needed for M1 (the harness would call the library in-process).

## Licence and constraints

- **Apache-2.0** for the model card metadata, footer ("Apache 2.0 · Convai Innovations"), the PyPI package and the GitHub repository sidebar. [HF][PyPI][GH] Apache-2.0 permits benchmarking, storing outputs and publishing results; the model card lists no other use restrictions and mentions none on benchmarking or output use.
- **Unverified:** the licence of the checkpoint files separately from the code (GH does not say) and the upstream licences of the base models (ModernBERT-large, mmBERT-base). Both should be confirmed before any adoption; neither blocks an offline benchmark.
- Because Apache-2.0 requires preserving notices when redistributing, we should not vendor weights into the repo (also ~800 MB). Download at run time.

## Which benchmark case types Laya can express

| Case type | Expressible? | How |
|---|---|---|
| **PAIR** (same fact or not) | Yes | `noul`, or preferably a two-option `choice` (`same`/`different`) given the `noul` label-bias caveat |
| **SUPPORT** (does the Passage support the statement) | Yes, within the token limit | Two- or three-option `choice` (`supports`/`contradicts`/`unrelated`) over statement plus Passage |
| **INDEPENDENCE** (are two Sources independent) | Yes | `choice` or `noul` over the pair of Source descriptions; likely metadata-driven, so code may do better. To be measured. |
| **GROUND** (exact quoted span, correct entity, metric, period) | **No** | Output is a decision, never text. At best `answer["window"]` locates a window. Evaluate GROUND only for baseline and the LLM-extraction candidate. |

## Open questions

For the owner:

1. Is the 512/1,024/8,192 context limit acceptable, i.e. may M1 run `laya-multilingual` at `max_len=8192` even though the card reports unstable accuracy beyond ~4,000 tokens?
2. May M1 add `laya` as a benchmark-only optional dependency (PyTorch is heavy), or should the harness stay dependency-free and read recorded decisions? The ticket text says "no new dependencies" for M1; this needs an explicit call before M2.
3. Is fine-tuning on our own labelled cases in scope for the spike, given the base checkpoints score near chance on typed decisions zero-shot? (If not, the benchmark compares only the fine-tuned `laya-typed-decisions` and the base checkpoints.)
4. Hardware for M2: CPU-only (200 to 460 ms per question) or GPU?

For Convai / the repo (read before M2):

5. Licence of the checkpoint files and of the ModernBERT/mmBERT bases.
6. Any telemetry in the `laya` package or `laya-serve`.
7. Contents of `SECURITY.md` and `BENCHMARKS.md`, and the training data behind the checkpoints.
8. Temperature-calibration procedure and expected size of the labelled set needed.
