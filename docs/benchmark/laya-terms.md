# Laya: API, licence and data-handling investigation (M1.1)

- **Ticket:** #29 (spike #26). **Verified:** 2026-10-10. No inference calls, installs or downloads were made; this is a read of public pages and metadata only.
- **Substitution.** #29 asked for the same investigation of Jev (TypeSafe AI, hosted). The owner chose **Laya** (Convai Innovations, open source, self-hosted). Jev remains only as the reference API Laya imitates. Where a Jev-shaped question (pricing, account retention) differs for a self-hosted model, the answer is given for Laya.
- **Method.** Pages were read through a summarising fetch tool, not a browser, and quotes below are as returned by it. Pages marked *not read in full* were truncated. Nothing here was run.

## Sources (all read 2026-10-10)

| Tag | Source |
|---|---|
| [HF] | Model card, https://huggingface.co/convaiinnovations/laya |
| [HF-API] | https://huggingface.co/api/models/convaiinnovations/laya (and `/laya-multilingual`) |
| [PyPI] | https://pypi.org/project/laya/ and https://pypi.org/pypi/laya/json (version 0.4.2, uploaded 2026-10-10) |
| [GH] | https://github.com/NandhaKishorM/laya (README: not read in full) |
| [LICENSE] | https://raw.githubusercontent.com/NandhaKishorM/laya/main/LICENSE |
| [SEC] | https://raw.githubusercontent.com/NandhaKishorM/laya/main/SECURITY.md |
| [BENCH] | https://raw.githubusercontent.com/NandhaKishorM/laya/main/BENCHMARKS.md |
| [Docs] | https://nandhakishorm.github.io/laya/ , `/questions-and-answers/`, `/reference/helpers/`, `/langchain/` |
| [MB-L] | https://huggingface.co/answerdotai/ModernBERT-large |
| [MB-B] | https://huggingface.co/jhu-clsp/mmBERT-base |

## What it is

A non-autoregressive "System 1" model: given a state (text, email, ticket or JSON) and typed questions, it returns answers with probabilities in one forward pass. "It never generates text, so there is nothing to parse and nothing to hallucinate." [HF] Three checkpoints: `laya` (English), `laya-multilingual`, `laya-typed-decisions`; a `Router` picks per request. [HF][PyPI]

## What the outputs can express

| Type | Output | Meaning |
|---|---|---|
| `choice` | `answers[q]["choice"]` | One label from a `criteria` dict |
| `score` | `answers[q]["score"]` | Position on an ordinal `criteria` list |
| `noul` | `answers[q]["noul"]` | P(true) over `[false, true]`; optional `labels` change only the displayed text, not polarity [Docs] |

- **No quoted spans, no free-form explanation.** The only locator is `answer["window"]` (the window that decided a long input), which is not a verbatim span. [GH] GROUND-style checks stay with code or the LLM extraction call.
- Probabilities are **not Evidence Confidence** (ADR-0005 rule 4). Base checkpoints are over-confident as shipped: temperature refit lowered ECE from 0.466 to 0.081 (`laya`) and 0.314 to 0.106 (`laya-multilingual`). [BENCH]

## Accuracy: what is and is not established

**Zero-shot semantic accuracy for our case types (PAIR, SUPPORT, INDEPENDENCE) is unproven.** Nothing published tests them. The nearest evidence:

- The only published zero-shot numbers on *typed decisions* are below the majority-class baseline: `laya` 0.362, `laya-multilingual` 0.352 vs majority 0.461 and random 0.318. "All of the capability on this benchmark comes from fine-tuning" (0.766 for `laya-typed-decisions`). [BENCH]
- English NLI (XNLI en) is higher: 0.860 / 0.843. XNLI is a related but different task from evidence equivalence, and the card does not say XNLI was held out of training (it marks only jailbreak, toxicity and routing as held out). Treat as a loose indicator, not a prediction. [BENCH]
- No similarity or paraphrase benchmark and no long-document accuracy table is reported in BENCHMARKS.md; the model card's long-document figures (16 to 18 of 20 up to ~4,000 tokens, then 8 to 17 of 20) are small samples. [BENCH][HF]
- `score` is the weakest primitive (SST-5 0.372). [HF]

## Effective state-token budget (defaults)

- Defaults from the checkpoint's `rl_agent_config.json`: `max_len = 512`, `head_max_len = 192`. [Docs: reference/helpers]
- The "head" is the question instructions plus one span per option, so **more options leave less room for the state**; each question in a request can have a different budget. [Docs]
- Default scan window for long inputs: `max(64, max_len - head_max_len - 8)` = **312 state tokens** at the defaults, stride 50% (~156). This is the *upper bound*: the real figure is smaller with more or longer options, and the docs give no fixed number per question (call `state_room`). [Docs]
- Tokens past the room are **silently cut** (the start is kept). `usage` reports `state_tokens_dropped`, `truncated`, `truncated_questions`; the harness must record these. [Docs]
- Other limits: `laya-typed-decisions` 1,024; `laya-multilingual` 1,024 by default, 8,192 with `max_len=8192`; server `LAYA_MAX_TOKEN_BUDGET` default 8,192; `choice` up to 100 options, batch up to 64 states. [HF][GH][Docs]
- Option count: the docs say accuracy "degrades quickly" past ~20 options (past ~40 with the default budget it "collapses"). Our questions have 2 to 3 options. [Docs]
- Implication: a Passage plus statement will often exceed ~312 tokens, so truncation is its own M1 failure class. A claim sitting after the cut-off is invisible to the model.

## Label-order and negation risks

- **Option order is positional**: the same labels in a different order are a different question. [Docs] Published order-flip rates: massive_intent.en 0.150 (`laya`) / 0.230 (`laya-multilingual`), emotion 0.040 / 0.090, xnli.en 0.000 / 0.015 (Jev: 0.13, third-party). [BENCH] Not measured on our cases.
- **Negation is unreliable in forced-choice questions**: a cancellation question can return the cancellation label for a state that says not to cancel, "with high confidence, on both checkpoints" (upstream issue #377). Negation sensitivity is not covered in BENCHMARKS.md. [Docs][BENCH] Relevant to SUPPORT (a Passage that negates the statement) and PAIR (not-the-same).
- **`noul` can follow its `false:`/`true:` labels over the state**, most strongly on the English checkpoint (upstream #156); `action.act_probability` has no usable signal (#185). [HF][Docs]
- Mitigations for M1 to test, not assumptions: randomise and swap option order and report the flip rate; include negation and near-miss cases; prefer a described two-option `choice` over `noul`; write option descriptions (bare labels give the model nothing). [Docs]

## Source independence (INDEPENDENCE)

Laya can classify **textual relationships** between two descriptions or texts, such as whether one reads as a syndicated or reprinted copy of the other. It **cannot independently establish source independence**: independence is a provenance fact (publisher, ownership, wire-service origin, original reporting, URL and date lineage), and without that metadata in the state the model sees only wording. Same-wire text under two mastheads and two genuinely independent reports of one fact can look alike or differ for unrelated reasons. M1 should treat Laya's INDEPENDENCE output as a syndication-text signal at most and compare it with a metadata-driven code baseline; the decision rule stays in code.

## Licence and provenance

- **Laya code and package: Apache-2.0**: [LICENSE] is the standard Apache-2.0 text (no copyright holder is named in the file as read); [PyPI] `license: Apache-2.0`, classifier Apache Software License; [GH] "Apache 2.0. Developed by Convai Innovations."
- **Laya checkpoints: Apache-2.0 per model-card metadata** (`license:apache-2.0`, tag `commercial-use`) for both `laya` and `laya-multilingual`. [HF-API] The README does not state per-checkpoint licences for the fine-tuned variants. [GH]
- **Upstream encoders**: **ModernBERT-large is Apache-2.0** [MB-L]; **mmBERT-base is MIT** [MB-B]. Both permit redistribution of derivatives with their notices.
- **Unresolved checkpoint provenance** (do not treat the above as a complete chain): the Laya model cards carry **no `base_model` or `datasets` metadata** [HF-API]; the README's claim that the English checkpoint is built on ModernBERT-large and the multilingual on mmBERT-base is from the repo, not from card metadata, and the parameter counts differ from the upstream cards (Laya `laya` ~421M vs ModernBERT-large ~395M), consistent with an added head but not documented; the training data is described only as mixes "in training" (e.g. AG News, BoolQ), with no full list and no licences for those datasets [BENCH][GH]. Whether training data licences affect weight licensing is **unresolved**.
- No restriction on benchmarking, storing outputs or publishing results appears in any source read. Apache-2.0 itself imposes none. Do not vendor weights (~800 MB; notice duties); download at run time, ideally by pinned revision (see below).

## Data handling and privacy

- Local inference is **supported**: SECURITY.md says Laya is "designed from the ground up for local, on-device execution", and the docs say it "runs on your own hardware". [SEC][Docs]
- **Not verified:** that the library and server make no outbound transmission or telemetry. None of SECURITY.md, the docs pages read, the model card or the (partial) README mentions telemetry, but absence of a statement is not absence of behaviour, and no source code, network trace or package contents were inspected. Besides weight downloads from the Hugging Face Hub, other outbound calls are undocumented. SECURITY.md also does not describe how inputs, outputs or logs are stored. Until checked (e.g. an offline run with network blocked, in M2), do not claim "nothing leaves the machine" for Source text.
- **No training-on-user-data statement** exists; moot for local inference, but also not a guarantee.
- Weight loading: SECURITY.md describes pinned revisions (`PINNED_REVISIONS`), SHA-256 verification (`LAYA_SHA256_DIGESTS`, `expected_sha256`), and safetensors or ONNX rather than pickles. Use them if adopted. [SEC]
- `laya-serve`: binds `0.0.0.0:8000` with **no authentication unless `LAYA_API_KEY` is set**; `LAYA_HOST=127.0.0.1` restricts it to loopback. Not needed for M1. [SEC]
- The Hugging Face demo Space is third-party hosted with undocumented retention: **do not send benchmark cases to it.**
- No account exists, so no account terms. Price: none per call (own compute); no reasoning-token or caching billing because nothing is generated. Latency (authors' T4 figures): 39.5 ms one question English, 32.8 ms multilingual; 193 to 464 ms preloaded on CPU; reload 7 to 10 s. [HF]
- Package: `laya` 0.4.2, Python 3.10+, core deps `torch>=2.0.0`, `transformers>=4.48.0`, `safetensors`, `huggingface_hub`, `numpy`. [PyPI] This would be a **new heavy dependency**, which M1 forbids; it is an M2 decision.

## Integrations

LangChain and LangGraph integrations **exist**: `pip install "laya[langchain]"` (installs `langchain-core` and `langgraph`) provides `LayaRouter`, `LayaGuardrail`, `LayaTriage`, `LayaEvaluator` and `LayaDecision`. [Docs: langchain] LlamaIndex, CrewAI, MCP and ONNX extras are also listed. [PyPI] **These are routing, screening and triage conveniences. They are not evidence that Laya is suitable as an Evidence Resolver**, and the docs themselves say router thresholds are uncalibrated as shipped. Our resolver contract would call the model directly, so they are irrelevant to M1.

## Which case types Laya can express

| Case type | Expressible? | Note |
|---|---|---|
| **PAIR** | Yes (accuracy unproven) | Described two-option `choice`; order swap and negation cases needed |
| **SUPPORT** | Yes, within the token room (accuracy unproven) | Statement plus Passage; truncation likely |
| **INDEPENDENCE** | Partly | Text relationship only; independence needs provenance metadata (see above) |
| **GROUND** | **No** | Output is a decision, not text; no spans |

## Recommended issue updates (not made here)

To be done by the owner or on request, keeping the M1/M2 boundaries (M1 offline: no calls, no new dependencies, no production wiring; Laya inference only from M2, which needs the dependency and hardware decisions below):

- **#29:** retitle to "M1.1: Laya API, licence and data-handling investigation"; point the output path at `docs/benchmark/laya-terms.md`; replace Jev wording in the acceptance criteria (account terms become self-hosted licence and privacy terms).
- **#26:** in "What is evaluated", change candidate 4 from "Jev (TypeSafe hosted System One)" to "Laya (open-source, self-hosted System One-style model, `POST /v1/systemone` compatible via `laya-serve`)"; update the title "(Jev vs Haiku vs Deterministic)" and the sub-ticket list accordingly. Jev may remain an optional extra comparison only if the owner wants it. ADR-0005 rule 6 and the "Nothing here adopts Jev" wording should be revisited in a separate docs change.
- Other M1 tickets that mention Jev (check #30 to #35) need the same substitution.

## Open questions

For the owner:

1. May M2 add `laya` (PyTorch) as an optional benchmark-only dependency, or should the harness read recorded decisions? (#29 forbids new dependencies in M1.)
2. Is fine-tuning on our labelled cases in scope? Zero-shot typed decisions score below the majority baseline.
3. Hardware for M2: CPU (193 to 464 ms per question) or GPU?
4. Is a network-blocked run acceptable evidence for the "no outbound transmission" claim?

For Convai / the repo:

5. Provenance: base models, full training datasets and their licences, and why parameter counts differ from upstream.
6. Any telemetry or outbound calls in `laya` and `laya-serve`.
7. Calibration procedure and the size of labelled set needed; behaviour on negation (issue #377) and `noul` label bias (#156).
