"""The deterministic baseline: the real T5/T6 production logic run on benchmark cases.

Every judgment here comes from a production function (`merge_identity`, `literal_problem`,
`sources_independent`, `fact_check`, `detect_conflicts`, ...); nothing is reimplemented. Where
production decides with a model (T5 drafting and entailment) the baseline does not guess: it
returns NOT_APPLICABLE and records the deterministic evidence it did compute as diagnostics.
Owner-approved mapping (M1.3, issue #31):

  PAIR          keys equal -> SAME_FACT; keys differ -> NOT_APPLICABLE (T5 only decides merge
                or no merge; the extraction conflict rule is recorded, never used as a label).
  SUPPORT       always NOT_APPLICABLE (support is an LLM entailment verdict); the lexical
                check and whether T5 would drop the item are recorded.
  GROUND        CLAIM_PASSAGE: the real `fact_check` (verified -> GROUNDED, unverified ->
                NOT_GROUNDED). QUOTED_SPAN: a failed literal check -> NOT_GROUNDED; a pass ->
                NOT_APPLICABLE (T5 has no span-support judgment).
  INDEPENDENCE  `sources_independent`: True -> INDEPENDENT, False -> DEPENDENT.

`fact_check` verifies by embedding similarity and a numeric check, which is not semantic
entailment. No model, network or fetch is used: the model slots of `Deps` are tripwires."""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from insightforge_agent.benchmark.cases import (
    GroundCheck,
    GroundingFailure,
    GroundInputs,
    GroundLabel,
    IndependenceInputs,
    IndependenceLabel,
    InputAccess,
    MergeFields,
    PairInputs,
    PairLabel,
    PredictionCase,
    SupportInputs,
)
from insightforge_agent.benchmark.judge import (
    DISPOSITION_KEY,
    REASON_KEY,
    JudgeStatus,
    Scalar,
    Usage,
    Verdict,
)
from insightforge_agent.benchmark.recording import RecordingStore, RunIdentity, StaleRecording
from insightforge_agent.domain.contracts import (
    Claim,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    ExtractedFact,
    PassageRef,
    ReportDraft,
    ReportSection,
)
from insightforge_agent.domain.evidence import cites, domain_of, sources_independent
from insightforge_agent.domain.extraction import detect_conflicts
from insightforge_agent.domain.passages import value_in_text
from insightforge_agent.domain.periods import UNKNOWN, period_stated_in, stated_period
from insightforge_agent.embeddings import HashingEmbedder
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.fact_check import fact_check
from insightforge_agent.pipeline.synthesize import literal_problem, merge_identity
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.source_store import PassageDraft, SourceStore

CANDIDATE = "deterministic-baseline"
CANDIDATE_VERSION = "m1.3-1"
MODEL = "none (production deterministic logic)"
SIMILARITY_THRESHOLD = 0.85  # the production default of Deps.similarity_threshold
EMBEDDING_DIMENSIONS = 256   # the production default HashingEmbedder
OWNER = "benchmark"
_FETCHED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# Evidence dispositions: what the deterministic part of T5 does to a drafted candidate.
MERGED = "MERGED"
NOT_MERGED = "NOT_MERGED"
DROPPED = "DROPPED_LITERAL_CHECK"            # no Passage passes the literal-value check
PENDING = "PASSES_LITERAL_CHECK_ENTAILMENT_NOT_RUN"  # a model would decide the rest

NO_ENTAILMENT = ("T5 decides support with a model entailment call; the deterministic checks "
                 "are only a necessary condition and cannot establish support")


def identity() -> RunIdentity:
    """Who the baseline is, for recording: a fixed version, and the settings that change its
    output (the similarity threshold and the embedder)."""
    return RunIdentity(
        candidate=CANDIDATE, candidate_version=CANDIDATE_VERSION, model=MODEL, run="",
        config={"provider": "none", "similarity_threshold": SIMILARITY_THRESHOLD,
                "embedder": "HashingEmbedder", "embedding_dimensions": EMBEDDING_DIMENSIONS},
    )


class _Tripwire:
    """Stands in for every Deps slot the baseline must never use (models, search, fetcher)."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __getattr__(self, name: str):
        if name.startswith("__"):  # copy, pickle and introspection probes are not use
            raise AttributeError(name)
        raise AssertionError(f"the deterministic baseline must not use {self._what} ({name})")


def _deps(repos: MemoryRepos, store: SourceStore) -> Deps:
    return Deps(
        repos=repos, store=store, planner_model=_Tripwire("a model"),
        writer_model=_Tripwire("a model"), light_model=_Tripwire("a model"),
        researcher_model=_Tripwire("a model"), search=_Tripwire("search"),
        fetcher=_Tripwire("a page fetcher"), checkpointer=None, now=lambda: _FETCHED_AT,
        embedder=HashingEmbedder(EMBEDDING_DIMENSIONS), similarity_threshold=SIMILARITY_THRESHOLD)


def _verdict(case: PredictionCase, status: JudgeStatus, *, label=None, reason=None,
             failure: GroundingFailure | None = None,
             diagnostics: dict[str, Scalar] | None = None) -> Verdict:
    # The baseline makes no model call: tokens are an explicit zero, not unknown.
    return Verdict(
        case_id=case.id, case_type=case.type, status=status, label=label, reason=reason,
        grounding_failure=failure, diagnostics=diagnostics or {},
        usage=Usage(input_tokens=0, output_tokens=0, retries=0, parse_failures=0))


def _reason_code(problem: str) -> str:
    return problem.split(":", 1)[0]


def _literal_diagnostics(problem: str | None) -> dict[str, Scalar]:
    if problem is None:
        return {"literal_check": "pass", DISPOSITION_KEY: PENDING}
    return {"literal_check": "fail", "literal_detail": problem, DISPOSITION_KEY: DROPPED,
            REASON_KEY: _reason_code(problem)}


def _failure_of(problem: str) -> GroundingFailure:
    return (GroundingFailure.UNSUPPORTED_NUMBER if problem.startswith("value not in Passage")
            else GroundingFailure.UNSUPPORTED_ENTITY)


class DeterministicBaseline:
    """A `Judge` that runs the real deterministic T5/T6 logic. It must be given BASELINE input
    access: only that view carries the structured fields T5's merge key is built from."""

    def judge(self, case: PredictionCase) -> Verdict:
        if case.access is not InputAccess.BASELINE:
            return _verdict(case, JudgeStatus.ERROR,
                            reason="the deterministic baseline needs BASELINE input access")
        try:
            return self._judge(case)
        except AssertionError:  # a tripwire: the baseline touched a model, search or fetcher
            raise
        except Exception as exc:  # noqa: BLE001 - recorded as ERROR, never turned into a label
            return _verdict(case, JudgeStatus.ERROR, reason=type(exc).__name__,
                            diagnostics={"error_detail": str(exc)[:300]})

    def _judge(self, case: PredictionCase) -> Verdict:
        inputs = case.inputs
        if isinstance(inputs, PairInputs):
            return self._pair(case, inputs)
        if isinstance(inputs, SupportInputs):
            return self._support(case, inputs)
        if isinstance(inputs, GroundInputs):
            return (self._claim_passage(case, inputs) if inputs.check is GroundCheck.CLAIM_PASSAGE
                    else self._quoted_span(case, inputs))
        if isinstance(inputs, IndependenceInputs):
            return self._independence(case, inputs)
        raise TypeError(f"unsupported case type {case.type}")

    # ---- PAIR ---------------------------------------------------------------------------

    def _pair(self, case: PredictionCase, p: PairInputs) -> Verdict:
        if p.structured_a is None or p.structured_b is None:
            return _verdict(case, JudgeStatus.NOT_APPLICABLE,
                            reason="the pair has no structured candidate fields, so T5's merge "
                                   "key cannot be formed")
        a, b = p.structured_a, p.structured_b
        key_a, key_b = _identity_of(p.statement_a, a), _identity_of(p.statement_b, b)
        diagnostics: dict[str, Scalar] = {
            "merge_identity_a": key_a, "merge_identity_b": key_b, "merge_key_equal": key_a == key_b}
        for side, statement, fields, passage in (
                ("a", p.statement_a, a, p.passage_a), ("b", p.statement_b, b, p.passage_b)):
            diagnostics.update(_passage_diagnostics(side, statement, fields, passage))
        if key_a == key_b:
            diagnostics[DISPOSITION_KEY] = MERGED
            return _verdict(case, JudgeStatus.JUDGED, label=PairLabel.SAME_FACT,
                            reason="T5 would merge these candidates: the merge keys are equal",
                            diagnostics=diagnostics)
        diagnostics[DISPOSITION_KEY] = NOT_MERGED
        diagnostics["extraction_conflict_rule"] = _conflict_rule(p, a, b)
        return _verdict(
            case, JudgeStatus.NOT_APPLICABLE, diagnostics=diagnostics,
            reason="T5 would not merge these candidates, but a nonmerge does not say whether "
                   "they are a different fact or a contradiction")

    # ---- SUPPORT ------------------------------------------------------------------------

    def _support(self, case: PredictionCase, s: SupportInputs) -> Verdict:
        problem = literal_problem(statement=s.statement, entities=[], entity="",
                                  passage_text=s.passage)
        return _verdict(case, JudgeStatus.NOT_APPLICABLE, reason=NO_ENTAILMENT,
                        diagnostics=_literal_diagnostics(problem))

    # ---- GROUND -------------------------------------------------------------------------

    def _quoted_span(self, case: PredictionCase, g: GroundInputs) -> Verdict:
        span = g.quoted_span or ""
        problem = literal_problem(statement=g.claim, entities=[], entity="", passage_text=span)
        diagnostics = _literal_diagnostics(problem)
        diagnostics["literal_check_passage"] = (
            "pass" if literal_problem(statement=g.claim, entities=[], entity="",
                                      passage_text=g.passage_text) is None else "fail")
        diagnostics["span_in_passage"] = span in g.passage_text  # a plain containment, not T5
        if problem is None:
            return _verdict(case, JudgeStatus.NOT_APPLICABLE, diagnostics=diagnostics,
                            reason="the lexical checks pass, but T5 has no judgment of whether a "
                                   "span supports a statement")
        return _verdict(case, JudgeStatus.JUDGED, label=GroundLabel.NOT_GROUNDED,
                        failure=_failure_of(problem), diagnostics=diagnostics,
                        reason=f"the quoted span fails T5's literal check ({problem})")

    def _claim_passage(self, case: PredictionCase, g: GroundInputs) -> Verdict:
        repos = MemoryRepos()
        store = SourceStore(repos.sources, repos.observations, repos.passages)
        # A document Source: its stored chunks are the original text, so the check needs no
        # fetch. The Passage is stored whole, as the case holds it.
        observation = store.ingest_document(
            owner_id=OWNER, run_id=case.id, filename="benchmark-case", fetched_at=_FETCHED_AT,
            content_hash=hashlib.sha256(g.passage_text.encode()).hexdigest(),
            chunks=[PassageDraft(g.passage_text)])
        ref = PassageRef(observation_id=observation.id, index=0)
        item = EvidenceItem(id="ev_benchmark", subtask_id="s", aspect_id="a", statement=g.claim,
                            confidence="LOW", supporting=[ref], primary=ref)
        bundle = EvidenceBundle(sections=[EvidenceSection(
            aspect_id="a", heading="a", confidence="LOW", items=[item])])
        draft = ReportDraft(title="benchmark", sections=[ReportSection(
            heading="a", claims=[Claim(text=g.claim, evidence_id=item.id, historical=g.historical)])])
        report = fact_check(_deps(repos, store), OWNER, case.id, draft, bundle)
        v = report.verdicts[0]
        diagnostics: dict[str, Scalar] = {
            "production_verdict": v.verdict, "check": v.check,
            "similarity_threshold": SIMILARITY_THRESHOLD, "semantic_entailment": False,
            **({"similarity": v.similarity} if v.similarity is not None else {})}
        if v.verdict == "unchecked":
            diagnostics["production_reason"] = v.reason
            return _verdict(case, JudgeStatus.NOT_APPLICABLE, diagnostics=diagnostics,
                            reason="T6 does not check a Historical Claim")
        if v.verdict == "verified":
            return _verdict(case, JudgeStatus.JUDGED, label=GroundLabel.GROUNDED,
                            diagnostics=diagnostics,
                            reason="T6 fact-check verified the Claim (similarity and numbers; "
                                   "not semantic entailment)")
        below = v.similarity is None or v.similarity < SIMILARITY_THRESHOLD
        return _verdict(
            case, JudgeStatus.JUDGED, label=GroundLabel.NOT_GROUNDED, diagnostics=diagnostics,
            failure=GroundingFailure.UNSUPPORTED_CLAIM if below
            else GroundingFailure.UNSUPPORTED_NUMBER,
            reason=f"T6 fact-check left the Claim unverified: {v.reason}")

    # ---- INDEPENDENCE -------------------------------------------------------------------

    def _independence(self, case: PredictionCase, i: IndependenceInputs) -> Verdict:
        a_texts, b_texts = [i.source_a_text], [i.source_b_text]
        independent = sources_independent(i.source_a_url, a_texts, i.source_b_url, b_texts)
        diagnostics: dict[str, Scalar] = {
            "same_domain": domain_of(i.source_a_url) == domain_of(i.source_b_url),
            "a_cites_b": cites(a_texts, i.source_b_url), "b_cites_a": cites(b_texts, i.source_a_url),
            "lexical_only": True}
        return _verdict(
            case, JudgeStatus.JUDGED, diagnostics=diagnostics,
            label=IndependenceLabel.INDEPENDENT if independent else IndependenceLabel.DEPENDENT,
            reason="T5's predicate (different domains, no link either way): "
                   + ("holds" if independent else "does not hold") + "; it is lexical only")


def _identity_of(statement: str, f: MergeFields) -> str:
    return merge_identity(statement=statement.strip(), entity=f.entity, predicate=f.predicate,
                          value=f.value, scope=f.scope, period=stated_period(f.period))


def _passage_diagnostics(side: str, statement: str, f: MergeFields,
                         passage: str | None) -> dict[str, Scalar]:
    if passage is None:
        return {}
    problem = literal_problem(statement=statement.strip(), entities=f.entities,
                              entity=f.entity.strip(), passage_text=passage)
    out: dict[str, Scalar] = {f"literal_{side}": problem or "pass"}
    if f.value.strip():
        out[f"value_stated_{side}"] = value_in_text(f.value.strip(), passage)
    period = stated_period(f.period)
    if period != UNKNOWN:
        out[f"period_stated_{side}"] = period_stated_in(period, passage)
    return out


def _conflict_rule(p: PairInputs, a: MergeFields, b: MergeFields) -> bool:
    """Whether the two candidates, as Extraction facts, would conflict under T5's structured
    rule (same group key, different value, overlapping period). Recorded, never a label:
    production also needs a model CONTRADICTED verdict."""
    def fact(tag: str, statement: str, f: MergeFields) -> ExtractedFact:
        return ExtractedFact(
            id=tag, source_id=tag, passage=PassageRef(observation_id=tag, index=0),
            statement=statement, entity=f.entity, predicate=f.predicate, scope=f.scope,
            value=f.value, period=stated_period(f.period))
    _, conflicts = detect_conflicts([fact("a", p.statement_a, a), fact("b", p.statement_b, b)])
    return bool(conflicts)


def run_baseline(cases: Sequence, store: RecordingStore, run: str) -> list[Verdict]:
    """Judge every case with BASELINE input access and record the Verdicts. Free and offline:
    no ledger. Re-running under the same run id reuses identical records; a changed case or
    baseline version under the same run id is refused (use a new run id)."""
    who = identity().model_copy(update={"run": run})
    judge = DeterministicBaseline()
    views = [case.prediction_view(InputAccess.BASELINE) for case in cases]
    stale = [f"{v.id}: {'; '.join(r)}" for v in views if (r := store.stale_reasons(who, v))]
    if stale:  # checked first, so a stale case never leaves a half-updated run
        raise StaleRecording(" | ".join(stale) + " (use a new run id)")
    verdicts = []
    for view in views:
        verdict = judge.judge(view)
        store.record(who, view, verdict)
        verdicts.append(verdict)
    return verdicts


__all__ = ["CANDIDATE", "DeterministicBaseline", "run_baseline"]
