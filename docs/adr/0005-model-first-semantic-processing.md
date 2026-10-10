# Semantic judgments are model-first; invariants stay in code

Status: proposed, pending owner approval. This records a direction. It changes no code and no accepted requirement, and it adopts no provider.

## Context

T5 verified Evidence with deterministic rules wherever a rule was cheap to write. The live qualification and the Confidence investigation showed the limit of that approach on natural language: many candidates were dropped on label-like entity names and cross-Passage numbers, and equivalent facts that were reworded, aliased or written with different qualifiers never corroborated each other, so no item reached HIGH. Each fix tends to add another regex, word list or special case (`quantities.py` is the clearest example). That path leads to an ever-growing pile of semantic heuristics that is hard to test and still wrong on ordinary language.

## Decision

Use the right tool for each problem, not deterministic code by default.

1. **Model-first for semantic understanding:** equivalence of entities, predicates, claims and values, paraphrase, entailment, ambiguous or contextual judgments, and temporal meaning. A capable model is the intended tool wherever an evaluation shows it is the better one.
2. **Deterministic code for system invariants:** orchestration, persistence, Passage identity and citation integrity, schema validation, exact arithmetic (`Decimal`, interval overlap), budgets and caps, ranking, and the Confidence policy.
3. **No growth of handwritten semantic heuristics without a written technical justification.** Each existing one is listed in the [rules ledger](../semantic-rules-ledger.md). A rule stays only with a reason that is not "deterministic feels safer".
4. **Models are evidence to verify, not facts.** A model proposes; code checks what can be checked (that cited Passages and spans exist, that arithmetic holds, that the output matches its schema) and applies policy. Model probabilities and scores are never Evidence Confidence. Passage text is untrusted web content, so a resolver has no tools, a strict output schema, and no ability to change policy.
5. **T12's controlled predicate vocabulary stays authoritative** for predicate cardinality and Finding identity. A model may resolve a natural-language predicate into that vocabulary; it does not replace the vocabulary's structural rules unless the architecture is explicitly revised.
6. **Evaluate before adopting.** Which tool wins is decided per problem by a human-reviewed benchmark (current code, Haiku and Laya, whose interface does not offer quoted spans, so span checks are evaluated separately). See the spike #26. Nothing here adopts Laya, Jev or any other resolver or provider.

## Migration approach

Contracts do not change: `EvidenceItem`, Passage references, provenance and Confidence semantics stay as they are.

- **M1:** benchmark and offline harness; no paid calls.
- **M2:** capped, approved evaluation (initial pilot at most $5).
- **M3 (starts only after a successful M2 and a separate design approval):** an `EvidenceResolver` interface with today's code as the deterministic implementation and a model-backed one in shadow mode, logging disagreements and affecting no output.
- **M4:** staged promotion per category, behind a setting, only where the evaluation gate passed and the design change is approved.
- **M5:** remove the heuristics a model path replaces; update the ledger so every remaining semantic rule carries its justification.

Existing tests become golden cases that run against both implementations. CI uses scripted fakes or recorded fixtures; live runs stay marked `live`.

## Consequences

- T6-T8 are not blocked. T6 ships its approved acceptance criteria (including the 0.85 similarity threshold and numeric check); its Claim-versus-Passage verification joins the evaluation, and any change needs a separate approved design change.
- T12 keeps its approved design. It may use an Evidence Resolver to map natural-language predicates into its controlled vocabulary only if the M2 evaluation succeeds and a separate design change is approved. Until then T12 does not depend on a resolver, and no resolver interface is assumed to be authorised.
- Until a category is promoted, the T5 deterministic behaviour remains in force and is not rewritten because of this ADR.
- Cost, latency, nondeterminism and provider dependence are accepted trade-offs to be measured, not assumed away.
