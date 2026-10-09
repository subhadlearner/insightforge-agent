## Agent skills

### Issue tracker

Issues are tracked as GitHub Issues (`gh` CLI) in github.com/subhadlearner/insightforge-agent. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Memory
Do not retrieve information from memory; always refer to durable source like GitHub for any information you might need. 

### Hygiene
Never ever commit directly to main. ALways create a feature branch and then raise a PR to main.


### Pull requests
Every PR description must start with a **Reviewer's guide**, written for someone who did not follow the implementation:
- **Requirement map**: a table of each acceptance criterion in the linked issue, the files that implement it, and the test that proves it. Mark any criterion not met.
- **Reading order**: the order to read the files in (contracts and domain first, then stages, then wiring, then tests), with one line on what each file is for.
- **Safe to skim**: files that are moved, generated, fixtures or plumbing, and why they carry little risk.
- **Deliberate limitations**: what was left out on purpose and which later ticket owns it.
Keep it factual and under about 60 lines. Link the issue with `Refs #N` and do not close it unless asked.
