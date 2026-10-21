# Documentation index

Start with the [README](../README.md). This index sorts the rest by what it is for.

## Current design

| Document | What it is |
|---|---|
| [design.md](design.md) | The system design: stages, contracts, stores, limits |
| [design-rationale.md](design-rationale.md) | Why the design is the way it is (questions and answers) |
| [adr/](adr/) | Architecture decisions 0001 to 0006; [ADR-0006](adr/0006-evidence-bound-support-and-requirements-ledger.md) is the current support and requirements contract |
| [semantic-rules-ledger.md](semantic-rules-ledger.md) | Every handwritten rule that makes a semantic judgment, why it exists and what is intended for it (ADR-0005) |
| [../CONTEXT.md](../CONTEXT.md) | The glossary every document uses |
| [agents/](agents/) | Conventions for coding agents: issue tracker, triage labels, domain docs |

## Verification records and audits (historical evidence)

These record what was run, on which commit, and what it showed, including the failures.
They are not rewritten when the code moves on.

| Document | What it records |
|---|---|
| [t18-verification.md](t18-verification.md) | The formal T18 Anthropic verification: B-1 FAIL, Stage C FAIL, Stage D not run |
| [audits/2026-10-21-research-redesign-verification.md](audits/2026-10-21-research-redesign-verification.md) | ADR-0006: offline verification, Gate A (FAILED), and the two capped live end-to-end Runs |
| [audits/2026-10-21-t18-b1-gate-exception.md](audits/2026-10-21-t18-b1-gate-exception.md), [audits/2026-10-21-t18-b1-historical-claim-audit.md](audits/2026-10-21-t18-b1-historical-claim-audit.md) | The B-1 gate failure and its accepted exception |
| [audits/](audits/) (2026-10-11 and 2026-10-12) | The Evidence-quality audit and its F2, F3 and F4 follow-ups |
| [t17-verification.md](t17-verification.md) | Fresh-clone setup and test results for the README |
| [t11-verification.md](t11-verification.md) | T11 Watchlist implementation and verification |
| [live-runs/](live-runs/) | T3 to T5 live runs with their recorded limits |

## Benchmark

| Document | What it is |
|---|---|
| [benchmark/methodology.md](benchmark/methodology.md) | The 48-case Evidence-quality benchmark: tiers, metrics, statistics |
| [benchmark/owner-review.md](benchmark/owner-review.md) | How the owner's blind review was run (the `bench review` CLI points here) |
| [benchmark/review-outcome.md](benchmark/review-outcome.md) | What the review produced; the second review was waived |
| [benchmark/laya-terms.md](benchmark/laya-terms.md) | The Laya API, licence and data-handling investigation (M1.1) |
| [archive/benchmark-review-integration-plan.md](archive/benchmark-review-integration-plan.md) | Superseded plan, kept for its rationale |

## Paths in historical records

On 2026-10-22 the repository moved to `src/backend/` and `src/frontend/`, and test modules
were renamed after the behaviour they verify. Records written before then (audits,
verification records, live-run notes, commit messages and issue comments) keep the paths and
commands that actually ran. Read them with this mapping:

| Before | After |
|---|---|
| `src/insightforge_agent/` | `src/backend/insightforge_agent/` |
| `tests/` | `src/backend/tests/` |
| `web/` | `src/frontend/` |
| `web/e2e/t15.spec.ts` | `src/frontend/e2e/watchlist-alerts-and-report-delivery.spec.ts` |
| `web/tests/t15-ui.test.tsx` | `src/frontend/tests/watchlist-alerts-and-sharing.test.tsx` |
| `docs/benchmark/review-integration-plan.md` | `docs/archive/benchmark-review-integration-plan.md` |
| `tests/api/test_t10_clarification_app.py` | `src/backend/tests/api/test_clarification_endpoints.py` |
| `tests/api/test_t10_research.py` | `src/backend/tests/api/test_research_submission.py` |
| `tests/api/test_t10_session_reports.py` | `src/backend/tests/api/test_persona_session_and_reports.py` |
| `tests/api/test_t10_sse.py` | `src/backend/tests/api/test_progress_stream.py` |
| `tests/api/test_t10_uvicorn_shutdown.py` | `src/backend/tests/api/test_uvicorn_graceful_shutdown.py` |
| `tests/api/test_t11_lifecycle.py` | `src/backend/tests/api/test_watchlist_scheduling.py` |
| `tests/api/test_t11_watchlist.py` | `src/backend/tests/api/test_watchlist_endpoints.py` |
| `tests/api/test_t12_diff_app.py` | `src/backend/tests/api/test_diff_alerts_app.py` |
| `tests/api/test_t14_citations.py` | `src/backend/tests/api/test_citations_endpoint.py` |
| `tests/api/test_t14_estimate.py` | `src/backend/tests/api/test_runtime_estimate_endpoint.py` |
| `tests/api/test_t15_delivery.py` | `src/backend/tests/api/test_report_export_and_shares.py` |
| `tests/api/test_t15_insights.py` | `src/backend/tests/api/test_watchlist_insights_endpoints.py` |
| `tests/api/test_t20_brief_limit.py` | `src/backend/tests/api/test_brief_length_limit.py` |
| `tests/api/test_t20_report_confidence.py` | `src/backend/tests/api/test_report_confidence_display.py` |
| `tests/api/test_t20_run_timing.py` | `src/backend/tests/api/test_run_timing_endpoint.py` |
| `tests/api/test_t20_scheduler_reliability.py` | `src/backend/tests/api/test_scheduler_reliability.py` |
| `tests/benchmark/test_contract_corrections.py` | `src/backend/tests/benchmark/test_pair_and_ground_contracts.py` |
| `tests/benchmark/test_integrity_corrections.py` | `src/backend/tests/benchmark/test_recording_and_ledger_integrity.py` |
| `tests/benchmark/test_report_corrections.py` | `src/backend/tests/benchmark/test_report_recorded_predictions.py` |
| `tests/domain/test_t12_diff.py` | `src/backend/tests/domain/test_finding_diff.py` |
| `tests/domain/test_t12_findings.py` | `src/backend/tests/domain/test_finding_identity.py` |
| `tests/domain/t12_builders.py` | `src/backend/tests/domain/finding_builders.py` |
| `tests/evaluation/test_t18_interactions.py` | `src/backend/tests/evaluation/test_remediation_changes_combined.py` |
| `tests/integration/test_f2_fact_check_bge.py` | `src/backend/tests/integration/test_fact_check_real_embedder.py` |
| `tests/integration/test_research_integrity_gate_plan.py` | `src/backend/tests/integration/test_historical_support_gate_plan.py` |
| `tests/integration/test_t18_acceptance_baseline.py` | `src/backend/tests/integration/test_historical_stage_c_baseline.py` |
| `tests/integration/test_t18_counterpart_replay.py` | `src/backend/tests/integration/test_historical_counterpart_replay.py` |
| `tests/integration/test_t18_document_replay.py` | `src/backend/tests/integration/test_historical_document_replay.py` |
| `tests/integration/test_t18_provenance_replay.py` | `src/backend/tests/integration/test_historical_provenance_replay.py` |
| `tests/integration/test_t18_stage_b_setup.py` | `src/backend/tests/integration/test_historical_stage_b_setup.py` |
| `tests/live/test_f2_real_model.py` | `src/backend/tests/live/test_fact_check_real_model.py` |
| `tests/live/test_f3_json_probe.py` | `src/backend/tests/live/test_drafting_output_ceiling_probe.py` |
| `tests/live/test_research_integrity_live.py` | `src/backend/tests/live/test_support_gate_live.py` |
| `tests/live/test_t18_stage_b.py` | `src/backend/tests/live/test_historical_stage_b_live.py` |
| `tests/pipeline/test_e2e2_source_dates.py` | `src/backend/tests/pipeline/test_source_dates.py` |
| `tests/pipeline/test_f2_counterpart.py` | `src/backend/tests/pipeline/test_fact_check_cited_passage.py` |
| `tests/pipeline/test_f2_fact_check_entailment.py` | `src/backend/tests/pipeline/test_fact_check_support.py` |
| `tests/pipeline/test_f4_false_merge.py` | `src/backend/tests/pipeline/test_evidence_merge_distinct_facts.py` |
| `tests/pipeline/test_research_integrity_analysis.py` | `src/backend/tests/pipeline/test_requirement_answers_and_analytical_claims.py` |
| `tests/pipeline/test_research_integrity_e2e1.py` | `src/backend/tests/pipeline/test_requirement_replies_and_claim_fallback.py` |
| `tests/pipeline/test_research_integrity_e2e2.py` | `src/backend/tests/pipeline/test_claim_omission_and_footnoted_quotes.py` |
| `tests/pipeline/test_research_integrity_requirements.py` | `src/backend/tests/pipeline/test_requirements_through_pipeline.py` |
| `tests/pipeline/test_research_integrity_review.py` | `src/backend/tests/pipeline/test_cited_passage_and_publication_checks.py` |
| `tests/pipeline/test_research_integrity_review2.py` | `src/backend/tests/pipeline/test_requirement_answer_strictness.py` |
| `tests/pipeline/test_research_integrity_review3.py` | `src/backend/tests/pipeline/test_evidence_compression_and_completeness.py` |
| `tests/pipeline/test_research_integrity_stage_c.py` | `src/backend/tests/pipeline/test_document_fixture_coverage.py` |
| `tests/pipeline/test_research_integrity_synthesis.py` | `src/backend/tests/pipeline/test_synthesis_support.py` |
| `tests/pipeline/test_research_integrity_work_units.py` | `src/backend/tests/pipeline/test_extraction_work_unit_recovery.py` |
| `tests/pipeline/test_t10_observation_provenance.py` | `src/backend/tests/pipeline/test_observation_provenance.py` |
| `tests/pipeline/test_t13_retention_pipeline.py` | `src/backend/tests/pipeline/test_retention_pipeline.py` |
| `tests/pipeline/test_t18_document_entailment.py` | `src/backend/tests/pipeline/test_document_identity_support.py` |
| `tests/pipeline/test_t18_document_fact_check.py` | `src/backend/tests/pipeline/test_document_identity_fact_check.py` |
| `tests/pipeline/test_t18_document_recovery.py` | `src/backend/tests/pipeline/test_document_reader_recovery.py` |
| `tests/pipeline/test_t18_forward_guidance.py` | `src/backend/tests/pipeline/test_forward_guidance_extraction.py` |
| `tests/pipeline/test_t18_report_persistence.py` | `src/backend/tests/pipeline/test_dated_report_persistence.py` |
| `tests/pipeline/test_t18_writer.py` | `src/backend/tests/pipeline/test_writer_instructions.py` |
| `tests/pipeline/test_t18_writer_fallback.py` | `src/backend/tests/pipeline/test_writer_claim_fallback.py` |
| `tests/pipeline/test_t20_report_structure.py` | `src/backend/tests/pipeline/test_report_structure.py` |
| `tests/pipeline/test_t20_subtask_atomic_runs.py` | `src/backend/tests/pipeline/test_subtask_plan_atomicity.py` |
| `tests/pipeline/test_t20_subtask_lifecycle.py` | `src/backend/tests/pipeline/test_subtask_lifecycle.py` |
| `tests/pipeline/test_t5_extractions.py` | `src/backend/tests/pipeline/test_synthesis_helper_extractions.py` |
| `tests/pipeline/test_t5_review.py` | `src/backend/tests/pipeline/test_extraction_synthesis_edge_cases.py` |
| `tests/pipeline/test_t6_writer_fact_check.py` | `src/backend/tests/pipeline/test_writer_and_fact_check.py` |
| `tests/pipeline/test_t7_concurrency.py` | `src/backend/tests/pipeline/test_researcher_concurrency.py` |
| `tests/pipeline/test_t7_documents.py` | `src/backend/tests/pipeline/test_uploaded_documents.py` |
| `tests/pipeline/test_t8_historical_checks.py` | `src/backend/tests/pipeline/test_historical_claim_checks.py` |
| `tests/pipeline/test_t8_memory.py` | `src/backend/tests/pipeline/test_memory_research.py` |
| `tests/pipeline/test_t9_clarification.py` | `src/backend/tests/pipeline/test_clarification.py` |
| `tests/stores/test_review_round2.py` | `src/backend/tests/stores/test_observation_and_report_durability.py` |
| `tests/stores/test_t12_diff_lifecycle.py` | `src/backend/tests/stores/test_diff_lifecycle.py` |
| `tests/stores/test_t12_webhook.py` | `src/backend/tests/stores/test_alert_webhook.py` |
| `tests/stores/test_t13_retention.py` | `src/backend/tests/stores/test_retention_sweep.py` |
| `tests/stores/test_t13_watchlist_regression.py` | `src/backend/tests/stores/test_retention_watchlist_baselines.py` |
| `tests/stores/test_t20_lifecycle_retention.py` | `src/backend/tests/stores/test_subtask_lifecycle_retention.py` |
| `tests/stores/test_t20_seed_retry.py` | `src/backend/tests/stores/test_baseline_seed_retry.py` |
| `tests/stores/test_t20_subtask_atomic.py` | `src/backend/tests/stores/test_subtask_state_atomicity.py` |
| `tests/stores/t12_world.py` | `src/backend/tests/stores/diff_world.py` |
| `tests/stores/t13_world.py` | `src/backend/tests/stores/retention_world.py` |
| `tests/test_research_integrity_gate.py` | `src/backend/tests/test_support_gate_decision.py` |
| `tests/test_t13_scheduler.py` | `src/backend/tests/test_retention_scheduler.py` |
| `tests/test_t18_acceptance.py` | `src/backend/tests/test_historical_acceptance_rules.py` |
| `tests/test_t18_stage_b_cases.py` | `src/backend/tests/test_historical_stage_b_cases.py` |
| `tests/test_t18_stage_b_support.py` | `src/backend/tests/test_historical_stage_b_harness.py` |
| `tests/test_t20_e2e_real_documents.py` | `src/backend/tests/test_web_e2e_real_documents.py` |
| `tests/test_t9_scheduler.py` | `src/backend/tests/test_clarification_scheduler.py` |

Test function names did not change, so a test id such as
`tests/pipeline/test_t18_writer.py::test_...` is found under the new file name with the same
function name.
