# InsightForge

An autonomous research platform: a user submits a research question and receives a cited
intelligence report. See `docs/design.md` for the design and `CONTEXT.md` for the vocabulary.
(The full README is T17.)

## Offline demo: Extraction and synthesis

Exercise the real Extraction and synthesis stages with seeded Sources and scripted models. No
API keys, no network, no extra dependencies:

```
uv run insightforge-agent demo
```

It prints the extracted facts and conflicts, the ranked Evidence with its supporting Sources,
Confidence labels and decision reasons, corroboration across number formats ("4.27 million" and
"4,270,000"), a contradiction between two Sources, and the candidates the pipeline rejects. It
ends with a PASS/FAIL list and exits non-zero if any expectation fails.

Only the model is scripted (`src/insightforge_agent/demo.py`); every decision shown is made by
the production pipeline.
