## Agent skills

### Issue tracker

Issues are tracked as GitHub Issues (`gh` CLI) in github.com/subhadlearner/insightforge-agent. See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Memory
Do not retrieve information from memory; always refer to durable source like GitHub for any information you might need. 
