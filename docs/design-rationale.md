# Design rationale: review grilling of 2026-10-08

Why the design changed on 2026-10-08, and what was considered and rejected. The decisions themselves live in `design.md`, `../CONTEXT.md` and `adr/`. This file keeps only the reasoning, the rejected options and the open threads that those documents leave out. Where this file and `design.md` disagree, `design.md` wins.

The session started from an external review of `design.md` that listed 7 gaps. It ran 32 questions (Q1 to Q32) and was followed by a second review with 3 findings.

## Verified against the assignment before deciding

- The Planner must be the `create_deep_agent()` root agent, with `write_todos`, spawning Researchers via `task` (assignment lines 94 to 95, rubric line 281). The old "supervisor deep agent" in `RESEARCHING` risked being graded non-compliant.
- Sub-tasks are 3 to 6 (line 91). Ranking is recency, then credibility, then relevance to the Brief (line 182). The citation drawer must show the exact paragraph, and this is a core requirement, not a stretch goal (line 248).
- Two contradictions were found in the old design:
  - "Only Synthesis reads raw content" contradicted Extraction reading all Sources.
  - "`doc_chunks` is Run-scoped" contradicted the uploaded-document fact-check and the citation drawer, which both need the chunks for as long as the Report exists.

## Decisions and their reasoning

### Planner and research (Q1 to Q3)

- **Q1: one Planner thread across `PLANNING` and `RESEARCHING`.** This is the most literal reading of "the Planner spawns the Researchers", and it lets `write_todos` really track Sub-task completion. The thread stays small because Researchers return only references.
  - Rejected: (b) two separate invocations, which is now the spike's fallback (Q32), and (c) merging both stages into one, which would break the live progress view.
  - We amended ADR-0001 and ADR-0003 rather than writing a new ADR.
- **Q2: one repair round, for missing results only.**
  - Missing and explicitly failed results are different. A Sub-task that failed explicitly is not retried, because the repair round exists to cover the model omitting a `task` call, not to retry failures.
  - No replanning, so the approved plan stays authoritative.
  - Idempotency, so a late result cannot overwrite a good one.
  - The repair round is the safety net for ADR-0003's distrust of model-driven parallelism.
- **Q3: one corrective re-prompt, then trim or fail.** The user added a coverage safeguard: trimming must keep coverage of the Brief. That led to Aspects (Q11).
  - Fewer than 3 Sub-tasks fails, even after a clarification timeout. A Report built on 1 or 2 Sub-tasks breaks the 3 to 6 contract, and passing it silently would hide that.

### Sources and clarification (Q4, Q5, Q13, Q14, Q31)

- **Q4: the availability matrix.** Reject with 422 when the only enabled source is unavailable, so an invalid Run fails before spending LLM tokens. The user's matrix:
  - Docs-only with no uploads: reject.
  - Memory-only with no Reports: reject.
  - Web plus unavailable Docs: proceed.

  Memory availability is checked against the User's own Reports, never the global collection.
- **Q5: the clarification timeout.** One compare-and-set guards against the answer and the timeout racing each other. The user added `RESUMING` as an internal recovery state, and the resolution is stored durably before resuming (Q15), so recovery after a crash is idempotent.
- **Q13: memory is background, never proof.** If a Watchlist Run could confirm facts from its own previous Report, it would re-assert stale facts and mask a real Material change. That would hurt the rubric's "genuine changes only". The user's worked example, which drives ADR-0004:

  | Run | What research finds | Outcome |
  |---|---|---|
  | A | Price $100 | Establishes the Baseline |
  | B | No pricing | Not reconfirmed, no Alert. B must not erase the $100 |
  | C | $130 | Alert: $100 → $130 |

  This also made Web Search mandatory for Watchlist Items: "a memory-only Watchlist would simply recycle existing intelligence".
- **Q14 to Q31: the `blocked` state was added, then removed.**
  - Q14 added a `blocked` Watchlist status for "no enabled source available".
  - The second review pointed out that it can never happen. Web is mandatory and always available, missing keys already stop the app at startup, and a provider outage already ends in a Failed Run with no Alert and the Baseline unchanged.
  - Q31 removed it, applying "don't build state transitions that cannot occur".
  - Rejected: a provider health check before each tick. It saves little, because the only LLM spend before research fails is planning, and it would need tuning against flaky false positives.

### Aspects (Q11, Q30)

- **Q11: Aspects make "essential coverage" checkable by code** rather than left to the LLM's judgement, and they give the Report's thematic sections a clean source.
  - Rejected: no trimming at all.
- **Q30: kept one Aspect per Sub-task (each Sub-task references exactly one Aspect; an Aspect may be covered by several Sub-tasks, for example with different source types).** The second review claimed Q11 had allowed several Aspects per Sub-task. That was wrong: Q11 decided one. The proposal was considered as a new decision instead: `aspect_ids[]` with a cap of 2 Aspects per Sub-task.
  - The user chose to keep one Aspect per Sub-task. Narrower Researcher scopes give more relevant retrieval, and ownership and progress tracking stay simple. Sub-tasks still map cleanly to Report sections, trimming and coverage checks stay simple, and it fits the assignment's maximum of 6.
  - Added: a corrective attempt to consolidate genuinely related Aspects, and a rule that a requested Aspect is never silently dropped.

### Ranking (Q6, Q16)

- **Q6: credibility is bucketed.** With a continuous credibility score, a strict lexicographic sort means relevance almost never comes into play. Bucketing honours the assignment's order and gives relevance real work. Sub-task priority stays as a cheap tie-break, and `evidence_id` makes the sort deterministic.
- **Q16: Confidence is deliberately not in the ranking tuple, because the assignment doesn't list it.** Accepted consequence: compression can drop a `HIGH` item ranked below a `LOW` one. To keep it visible, the compression log records each dropped item's Confidence.
  - Relevance influences ranking but never overrides provenance, independence or Confidence.

### Baseline and diffing (Q7, Q12, Q20 to Q24)

- **Q7: the Baseline advances only after a successful Diff.** If it advanced after a failed Diff, a change in B would be lost forever once C is compared against B.
  - A→C is a net change, and the UI must not imply that it lists every change that happened during B.
  - No manual re-diff, to keep the scope down.
- **Q12: no Report deletion in this build.** Deletion would open cascades: a deleted Baseline, a memory chain back to a deleted Report's Sources, Alerts linking to deleted Reports. Without deletion, retention only ever removes material nothing cites, so no reference can dangle.
- **Q20: ADR-0004 was written** because diffing against per-Finding state passes all three ADR tests: hard to reverse, surprising, and a real trade-off.
- **Q21: predicates declare cardinality.** Without it, "X competes with Z" would read as a change from "X competes with Y".
- **Q22: polarity on Findings.** Removal requires affirmative evidence, meaning a fresh replacement value or an explicit negated Finding. Silence is never evidence.
- **Q23: escalation for "Not reconfirmed" is display only.** A Finding that is Not reconfirmed for a long time is probably out of date, but silence still isn't evidence, so it raises no Alert.
- **Q24: an Alert links to the Report that established each earlier value,** because once the Baseline stopped being a Report, "links to both Reports" no longer meant anything.

### Provenance and evidence (Q8, Q17, Q19, Q25, Q26)

- **Q8: Passage as a domain term.** "Exact paragraph" needs an addressable paragraph. With it, the citation drawer resolves for every Claim without depending on the Fact-Checker's 20% sample.
  - The user's refinements:
    - Passage IDs stay stable, and an Observation is never re-split, so changes to parsing never break a citation.
    - The primary Passage must actually substantiate the item, so an authoritative but vaguely related Passage cannot become the citation.
    - Independent Sources are counted over distinct Sources, never Passages: "five Passages from one article still represent one Source".
- **Q17: the user rejected a deterministic-only check.** Literal-value matching alone (numbers and named entities present) can pass a Passage that doesn't actually support the item. So the check runs deterministic provenance and literal-value checks first, then a semantic entailment verdict.
- **Q19: for PDFs, Passage = Docling chunk.** Search, fact-checking and citations then use one unit, and nothing is re-split. Accepted cost: the drawer may highlight a chunk spanning several paragraphs.
- **Q25: one bounded entailment call per Evidence item,** run only after the deterministic checks, so most junk never reaches the model and the cost stays under the Run's token cap.
- **Q26: the user rejected "a contradiction from the same Source doesn't count".** Source independence does not make a genuine contradiction disappear. What matters is meaning and context: same entity, predicate and scope, with overlapping periods.

### Time (Q28, Q29)

- **Q28: the user rejected "different periods are distinct Findings".** It would have stopped the Diff from ever seeing $100 (2024) → $130 (2026) as a change, which is the change it exists to catch. Hence the As-of period is never part of a Finding's identity, and an older value never overwrites a newer one.
- **Q29: derived periods.** Pricing, product and leadership pages, the Sources most likely to show a change, usually state no period and have no publication date. A strict "unknown means ambiguous" rule would make watchlists miss exactly what they exist to catch.
  - Rejected: (a) strict rule 6, where every unknown period is flagged and never counts as a change.
  - The user added that the derivation applies only to present-state assertions. They also added the detected-versus-changed wording: an observation date says when information was seen, not when the change happened.

### Context engineering and retention (Q9, Q10, Q15, Q18, Q27)

- **Q9: the raw-text access table.** It resolved the "only Synthesis reads raw content" contradiction. The user added explicit Fact-Checker access, because Q8 needs the Fact-Checker to read Passages.
  - The per-call cap and the 6,000-token bundle budget are separate limits and must be tested separately. Context engineering is 20% of the grade, and the pass threshold requires at least 60% on it.
- **Q18: the fetch tool writes the one-line summary.** Otherwise 5 full article bodies would sit in the Web Search researcher's context, enough to blow it up on their own. It also makes "Researchers access raw content only inside their tools" literally true, and it's a ready example for the README's context-blowout writeup.
- **Q10: retention works on references, not Run status.** Failed Run material is kept for N days for debugging. A small failure record survives cleanup, so failures can still be analysed without storing raw web content or PDFs forever.
- **Q15: a crash during `RESUMING` is resumed, not failed,** because the resolution was stored before the state change.
- **Q27: Historical Claims can be sampled by the Fact-Checker,** checked through the memory inheritance chain, because they appear in the Report and their accuracy matters too.

### The spike (Q32)

- The integration was confirmed as unproven. The pass criteria and fallback are in `design.md` §12.
- The fallback is deliberately **not** written into ADR-0001 yet. Recording a fallback that hasn't happened would put a decision in the ADR that nobody has made, so ADR-0001 changes only if the spike fails.
- The share of parallel calls that arrive in one turn is measured, not required, because the repair round covers missing calls either way.

## Open threads

- **Library facts are not verified.** The design's §12 lists facts that the external review reports as confirmed. None of them were checked against installed packages in this session.
  - The most doubtful is whether `write_todos` / `TodoListMiddleware` is opt-in. The design assumed opt-in, but current `deepagents` may include it by default.
- **ADR-0001's filename** (`…planner-as-node.md`) no longer matches its title. It was kept so existing references don't break.
- **Tooling and commits.** Resolved: `gh` is installed and the docs are committed. See the next-steps section below.

## Next-steps grilling of 2026-10-08 (Q1 to Q17)

A second session derived the next steps from the documents above. The docs were already committed and `gh` is installed and authenticated. Where this section and the older text disagree, this section and `design.md` win.

### Decisions and their reasoning

- **Spike verifies before it proves.** Ticket 0 is a minimal scaffold. Ticket 1 verifies library facts (`write_todos` defaults, checkpointing, interrupt and resume, subagent isolation) against installed versions and then runs the integration proof. A separate investigation between tickets would be a manual dependency, and the checks are only relevant to the spike.
- **Free-tier development.** Gemini free tier by default (Groq selectable), local `fastembed` embeddings, Tavily with SerpAPI fallback. Anthropic only for the final verification. Embeddings stay the same across providers, and Anthropic has no embedding API.
- **Fake models by default, `live` marker for real ones.** Free tiers are rate-limited and vary in tool-calling quality, so the default suite must be deterministic. The spike and the happy-path ticket are the exceptions and need a live run.
- **Provider-dependent results are recorded per provider.** The spike's criteria 1 to 3 must pass on Anthropic as well, because Gemini passing proves nothing about Anthropic compatibility.
- **Next.js frontend added to the design.** The assignment mandates it, and the design was silent. A `web/` directory with an `/api/*` rewrite proxy keeps one origin for the cookie and SSE.
- **No `/to-spec`.** `design.md` is the spec, so tickets cite its sections.
- **Stretch goals and bonuses are out of scope.** Citation drawer is not the citation-trace bonus, and an optional LangSmith setting is not the tracing bonus. LangSmith was removed from ticket 0.
- **`max_runs` on Watchlist Items.** Allows a short demo cadence without unbounded provider calls. A field, not a status.

### Tickets (to be created with `/to-tickets`)

| # | Ticket | Blocked by |
|---|---|---|
| 0 | Scaffold: dependencies, `Settings`, `build_chat_model`, package skeleton, import-linter, `.env.example`, `docker compose` for Qdrant | none |
| 1 | Spike: verify library facts, then the checkpointed Planner thread (§12) | 0 |
| 2 | Persistence core: SQLite repositories, `SourceStore`, `run_events`, personas | 0 |
| 3 | Thin web-only Brief to Report path. Acceptance includes one successful live run on the dev provider | 1, 2 |
| 4 | Planner bounds, Aspects, source availability check and 422 | 3 |
| 5 | Extraction and synthesis: entailment, Confidence, ranking. Acceptance names bounded Passage retrieval, isolation, the 6,000-token bundle, compression logs, and payload-capture tests for the cap and the budget | 3 |
| 6 | Writer, Fact-Checker and citation Passages | 5 |
| 7 | Document ingestion (Docling) and the Doc Reader. Adds its eval brief | 3 |
| 8 | Memory researcher and Run summaries. Adds its eval brief | 6 |
| 9 | Clarification: interrupt, timeout job, `RESUMING`, startup sweep | 4 |
| 10 | API: `/research/*`, SSE with cursor, `/reports` list and retrieve, persona auth | 3 |
| 11 | Watchlist CRUD (with `max_runs`), ticks and Baseline | 2, 6 |
| 12 | Diff Agent, Findings, Alerts, Slack-format webhook | 11 |
| 13 | Retention sweep | 6, 7 |
| 14 | Next.js UI: brief input, live progress, report viewer, citation drawer | 10 |
| 15 | Watchlist dashboard, Diff view, alerts, plus the export, PDF and share-link endpoints | 11, 12, 14 |
| 16 | Sample brief library (first step), eval command, evaluation report | 6 |
| 17 | README. Starts early and grows as features land. Final acceptance embeds the Anthropic results | final acceptance after 18 |
| 18 | Final Anthropic verification (`ready-for-human`): all 5 briefs, spike criteria 1 to 3 re-run, results recorded, document and memory paths actually exercised in the evaluation, under 80% fact-check pass rate triggers a documented remediation | 7, 8, 9, 12, 15, 16 |
| 19 | Demo (5 to 7 minutes) and LinkedIn post. Preparation starts early, with a demo Watchlist Item that sets `max_runs`. Checklist: repo is public. Final acceptance after 18 | final acceptance after 18 |

Retention (#13) does not block #18. Document ingestion (#7) does, so the evaluation cannot run without a working document path. After the spike, only persistence (#2) and the frontend scaffold run alongside it. The API (#10) needs #3, and scheduling (#11) needs #6.

### Rubric map

| Rubric item | Weight | Proven by |
|---|---|---|
| Multi-agent architecture | 20%, gate at 60% | #1, #3, #4, #9 |
| Context engineering | 20%, gate at 60% | #5, #3, #6 |
| Report quality | 20% | #6, #16 |
| Fact-check pass rate (at least 80%) | 15% | #6, #16, #18 |
| Scheduler and diff | 10% | #11, #12 |
| UI completeness | 10% | #10, #14, #15 |
| Code quality | 5% | #0 and every ticket |

Deliverables that had no owner and now do: the sample brief library (#16), Slack-format webhook (#12), and the demo and post (#19). Entity extraction is LLM-based, which the README states.

### Execution

Run `/implement #<n>` per ticket in a fresh session. A ticket can start when it is open, labelled `ready-for-agent`, and has no open blockers in GitHub's native "Blocked by". Close each ticket when it's done, because blockers only clear when closed.
