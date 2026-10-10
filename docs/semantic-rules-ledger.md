# Semantic rules ledger

Every handwritten rule that makes a semantic judgment, with why it exists and what is intended for it. Maintained under [ADR-0005](adr/0005-model-first-semantic-processing.md); the evaluation is [#26](https://github.com/subhadlearner/insightforge-agent/issues/26). Adding a row needs a justification. Dispositions are intentions to be tested, not decisions: "candidate" means the evaluation may show a model is better; nothing changes until a design change is approved.

Disposition key: **keep** (belongs in code: invariant, arithmetic, policy), **candidate** (semantic; evaluate a model), **owned elsewhere** (a ticket already decides it), **approved criterion** (accepted requirement, unchanged until a separate approval).

| # | Rule | Where | What it decides | Why handwritten today | Disposition |
|---|---|---|---|---|---|
| 1 | Entity literal check: declared entities plus capitalised non-initial words must appear in the Passage | `pipeline/synthesize._literal_problem`, `domain/passages.named_entities_in` | Whether a Passage may support an item | Cheap, deterministic guard against invented names (T5) | candidate; high drop rate seen in the T5 live run, mostly label-like entity names |
| 2 | Number literal check: statement figures must be stated in the Passage | `domain/quantities.unsupported_numbers` | Same, for figures | Guards against invented numbers | split: the exact `Decimal` comparison is **keep**; reading units, magnitudes and what a figure measures is **candidate** |
| 3 | Number parsing and unit vocabulary (`_NOT_UNITS`, currency, percent) | `domain/quantities.py` | What a written number means | Needed for exact numeric equivalence (T5) | candidate for the parsing and vocabulary; arithmetic stays |
| 4 | Structured value support (`value_in_text`) | `domain/passages.py` | Whether a structured value is stated in the Passage | Stops fabricated values driving conflicts | candidate (lexical stand-in for "does the Passage state this"); exact quantity equality is keep |
| 5 | Stated period support (`period_stated_in`) | `domain/periods.py` | Whether the text states a period | Stops fabricated periods driving ranking | candidate |
| 6 | Period parsing and overlap (`parse_period`, `periods_overlap`) | `domain/periods.py` | Interval arithmetic | Exact calendar arithmetic | **keep** |
| 7 | Merge key: normalised entity, predicate, scope, period, canonical value (else normalised statement) | `pipeline/synthesize._draft` | Which drafted candidates are the same Evidence item | Exact-key dedupe is simple and safe (T5) | candidate; reworded, aliased, synonym or qualified forms do not merge, so corroboration and HIGH Confidence are rare |
| 8 | Challenger discovery and conflict grouping by equal `group_key` | `pipeline/synthesize`, `domain/extraction.detect_conflicts` | Which Passages might contradict an item | Needs entity/predicate/scope equality | candidate for the equivalence; the period-overlap test is keep; the predicate vocabulary is owned by T12 |
| 9 | Contradiction context check | `pipeline/synthesize._validate` | Whether a `CONTRADICTED` verdict is a genuine conflict | Spec: same entity, predicate, scope, overlapping period, different value | the verdict itself is already model-made; the entity/predicate/scope equivalence inside is candidate |
| 10 | Source independence: different domain and no textual mention of the other | `pipeline/synthesize`, `domain/evidence.cites` | Whether two Sources are Independent Sources | No citation graph is stored | candidate (syndication is missed); domain extraction is keep |
| 11 | As-of derivation rule | `pipeline/synthesize._validate` | When the Observation date stands in for a missing period | Specified policy (design §5) | **keep** as policy; the present-state judgement it uses is already model-made in the entailment call |
| 12 | Entailment verdicts (SUPPORTED / CONTRADICTED / INSUFFICIENT) | entailment call | Support and contradiction | Already a model call | not a handwritten rule; the evaluation compares models here |
| 13 | Claim-versus-Passage verification: similarity >= 0.85 and numeric check | T6 Fact-Checker (design §7) | Claim verdicts | Approved acceptance criteria | **approved criterion**; in the evaluation, unchanged until a separate approved design change |
| 14 | Controlled predicate vocabulary, cardinality and Finding identity | T12 (design §8) | Finding identity | Structural rules | **owned elsewhere** and authoritative; a model may map predicates into it |
| 15 | Credibility score from domain type and recency | `agents/web.credibility_score` | Source credibility | First simple score; byline and date extraction deferred | owned by the later credibility work; a policy input, not evaluated here |
| 16 | Confidence rules, ranking tuple, bucket thresholds, budget compression, windowing, token caps | `domain/evidence.py`, `domain/passages.py` | Policy and limits | Explicit policy and invariants | **keep** |
| 17 | Writer name check: capitalised non-initial words in a Claim must appear in the cited item's statement or entity | `pipeline/write.violations`, `domain/passages.named_entities_in` | Whether a Claim invents a name | Approved T6 criterion ("every number and named entity appears in the item"); reuses row 1's cheap check | **approved criterion**; shares row 1's false-positive risk, in the evaluation |

Notes:

- Rows 1-5, 7, 8 and 10 are where a model could matter most for corroboration and for dropped candidates in the T5 live run. The run's drops were mostly name-related (about two thirds), with number-related drops next; the log does not separate the sub-causes further.
- A model never sets Confidence. Where a model replaces a rule, its output is checked (identifiers and spans exist, arithmetic is done in code) and the Confidence policy runs on the verified result.
