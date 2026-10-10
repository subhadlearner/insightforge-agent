"""Tier 1: the real deterministic production baseline. No model, no fixture of predictions: the
adapters call the genuine T5/T6 functions and the expected values below follow from production
behaviour (docs/benchmark/methodology.md, "Baseline mapping")."""

import pytest

from insightforge_agent.benchmark import baseline as b
from insightforge_agent.benchmark.cases import InputAccess
from insightforge_agent.benchmark.judge import JudgeStatus
from insightforge_agent.benchmark.recording import RecordingStore
from insightforge_agent.stores.memory import MemoryRepos
from insightforge_agent.stores.source_store import SourceStore

from .helpers import make_case

pytestmark = pytest.mark.baseline

BASE = InputAccess.BASELINE


def judge(case):
    return b.DeterministicBaseline().judge(case.prediction_view(BASE))


def fields(**kw):
    return {"entity": "BYD", "predicate": "units_sold", "value": "4.27 million",
            "scope": "global", "period": "2025", **kw}


def pair(a, other, case_id="p", **inputs):
    return make_case("PAIR", case_id, inputs={
        "statement_a": "BYD sold 4.27 million vehicles in 2025.",
        "statement_b": "BYD delivered 4,270,000 cars in 2025.",
        "structured_a": a, "structured_b": other, **inputs})


# ---- PAIR ------------------------------------------------------------------------------------

def test_equal_merge_keys_are_the_same_fact():
    v = judge(pair(fields(), fields(value="4,270,000")))
    assert v.status is JudgeStatus.JUDGED and v.label == "SAME_FACT"
    assert v.diagnostics["merge_key_equal"] is True and v.diagnostics[b.DISPOSITION_KEY] == b.MERGED


def test_a_nonmerge_is_not_applicable_and_never_a_different_fact_or_contradiction():
    # Same group, different value, same period: T5's structural conflict rule would fire, yet
    # the baseline records it and still emits no label.
    v = judge(pair(fields(), fields(value="4.01 million")))
    assert v.status is JudgeStatus.NOT_APPLICABLE and v.label is None
    assert v.diagnostics["extraction_conflict_rule"] is True
    assert v.diagnostics[b.DISPOSITION_KEY] == b.NOT_MERGED
    other = judge(pair(fields(), fields(predicate="deliveries", value="1")))
    assert other.status is JudgeStatus.NOT_APPLICABLE
    assert other.diagnostics["extraction_conflict_rule"] is False


def test_a_different_stated_period_is_a_different_merge_key():
    v = judge(pair(fields(), fields(period="2024")))
    assert v.status is JudgeStatus.NOT_APPLICABLE and v.diagnostics["merge_key_equal"] is False


def test_a_pair_without_structured_fields_cannot_form_a_merge_key():
    v = judge(make_case("PAIR", "p"))
    assert v.status is JudgeStatus.NOT_APPLICABLE and "structured" in v.reason


def test_pair_passage_diagnostics_use_the_production_checks():
    v = judge(pair(fields(), fields(), passage_a="BYD sold 4.27 million vehicles in 2025.",
                   passage_b="BYD sold 3 million vehicles."))
    d = v.diagnostics
    assert d["literal_a"] == "pass" and d["value_stated_a"] is True and d["period_stated_a"] is True
    assert d["literal_b"].startswith("value not in Passage")
    assert d["value_stated_b"] is False and d["period_stated_b"] is False


# ---- SUPPORT ---------------------------------------------------------------------------------

def test_support_is_never_judged_and_records_the_real_drop_outcome():
    ok = judge(make_case("SUPPORT", "s", inputs={
        "statement": "Acme revenue was $5M.", "passage": "Acme reported revenue of $5M."}))
    assert ok.status is JudgeStatus.NOT_APPLICABLE and ok.label is None
    assert ok.diagnostics["literal_check"] == "pass"
    assert ok.diagnostics[b.DISPOSITION_KEY] == b.PENDING
    dropped = judge(make_case("SUPPORT", "s2", expected="CONTRADICTED", inputs={
        "statement": "Acme revenue was $9M.", "passage": "Acme reported revenue of $5M."}))
    assert dropped.status is JudgeStatus.NOT_APPLICABLE and dropped.label is None
    assert dropped.diagnostics[b.DISPOSITION_KEY] == b.DROPPED
    assert dropped.diagnostics[b.REASON_KEY] == "value not in Passage"


# ---- GROUND ----------------------------------------------------------------------------------

def ground(check, claim, passage, span=None, **kw):
    inputs = {"check": check, "claim": claim, "passage_text": passage,
              "passage_ref": {"observation_id": "o", "index": 0}, **kw}
    if span is not None:
        inputs["quoted_span"] = span
    return make_case("GROUND", "g", expected="NOT_GROUNDED", inputs=inputs)


def test_quoted_span_with_an_unsupported_number_is_not_grounded():
    v = judge(ground("QUOTED_SPAN", "Acme revenue grew 12%.", "Acme revenue grew 15% in 2024.",
                     span="revenue grew 15%"))
    assert v.status is JudgeStatus.JUDGED and v.label == "NOT_GROUNDED"
    assert v.grounding_failure == "UNSUPPORTED_NUMBER"


def test_quoted_span_with_an_unsupported_name_is_not_grounded():
    v = judge(ground("QUOTED_SPAN", "Acme Corp sold 5 units.", "Beta sold 5 units in 2024.",
                     span="sold 5 units"))
    assert v.label == "NOT_GROUNDED" and v.grounding_failure == "UNSUPPORTED_ENTITY"


def test_a_quoted_span_that_passes_the_lexical_check_is_not_applicable():
    v = judge(ground("QUOTED_SPAN", "Acme revenue grew 10%.", "Acme revenue grew 10% in 2024.",
                     span="revenue grew 10%"))
    assert v.status is JudgeStatus.NOT_APPLICABLE and v.label is None
    assert v.diagnostics["span_in_passage"] is True


def test_claim_passage_runs_the_real_fact_check_and_verifies_a_matching_claim():
    text = "BYD sold 4.27 million battery electric vehicles in 2025."
    v = judge(ground("CLAIM_PASSAGE", text, text))
    assert v.status is JudgeStatus.JUDGED and v.label == "GROUNDED"
    assert v.diagnostics["production_verdict"] == "verified"
    assert v.diagnostics["semantic_entailment"] is False
    assert v.diagnostics["similarity"] >= 0.85


def test_claim_passage_below_the_similarity_threshold_is_unverified():
    v = judge(ground("CLAIM_PASSAGE", "Tesla opened a factory in Berlin.",
                     "BYD sold 4.27 million battery electric vehicles in 2025."))
    assert v.label == "NOT_GROUNDED" and v.grounding_failure == "UNSUPPORTED_CLAIM"
    assert v.diagnostics["similarity"] < 0.85


def test_claim_passage_keeps_the_numeric_check_apart_from_similarity():
    # Near-identical text, so similarity clears 0.85, but one figure differs.
    passage = "BYD sold 4.27 million battery electric vehicles worldwide during the whole of 2025."
    claim = "BYD sold 4.31 million battery electric vehicles worldwide during the whole of 2025."
    v = judge(ground("CLAIM_PASSAGE", claim, passage))
    assert v.diagnostics["similarity"] >= 0.85
    assert v.label == "NOT_GROUNDED" and v.grounding_failure == "UNSUPPORTED_NUMBER"


def test_a_historical_claim_is_not_checked_by_production_and_so_not_applicable():
    text = "BYD sold 4.27 million vehicles in 2025."
    v = judge(ground("CLAIM_PASSAGE", text, text, historical=True))
    assert v.status is JudgeStatus.NOT_APPLICABLE and v.label is None


# ---- INDEPENDENCE ----------------------------------------------------------------------------

def independence(a_url, b_url, a_text="Acme grew.", b_text="Acme expanded.", case_id="i"):
    return make_case("INDEPENDENCE", case_id, inputs={
        "source_a_url": a_url, "source_a_text": a_text,
        "source_b_url": b_url, "source_b_text": b_text})


def test_independence_follows_the_production_predicate():
    assert judge(independence("https://a.example/x", "https://b.test/y")).label == "INDEPENDENT"
    same = judge(independence("https://a.example/x", "https://www.a.example/y"))
    assert same.label == "DEPENDENT" and same.diagnostics["same_domain"] is True
    cited = judge(independence("https://a.example/x", "https://b.test/y",
                               a_text="per https://b.test/y"))
    assert cited.label == "DEPENDENT" and cited.diagnostics["a_cites_b"] is True


# ---- contract --------------------------------------------------------------------------------

def test_the_baseline_refuses_a_semantic_view():
    case = make_case("PAIR", "p")
    v = b.DeterministicBaseline().judge(case.prediction_view(InputAccess.SEMANTIC))
    assert v.status is JudgeStatus.ERROR and "BASELINE" in v.reason


def test_the_baseline_reports_explicit_zero_usage_and_no_model_scores():
    v = judge(independence("https://a.example/x", "https://b.test/y"))
    assert (v.usage.input_tokens, v.usage.output_tokens, v.usage.retries) == (0, 0, 0)
    assert v.model_scores is None


def test_a_tripwire_is_not_swallowed_into_an_error_verdict(monkeypatch):
    def touch_model(self, case, g):
        raise AssertionError("the deterministic baseline must not use a model (invoke)")

    monkeypatch.setattr(b.DeterministicBaseline, "_claim_passage", touch_model)
    case = ground_claim()
    with pytest.raises(AssertionError, match="must not use"):
        judge(case)


def ground_claim():
    text = "BYD sold 4.27 million vehicles in 2025."
    return make_case("GROUND", "g", expected="GROUNDED", inputs={
        "check": "CLAIM_PASSAGE", "claim": text, "passage_text": text,
        "passage_ref": {"observation_id": "o", "index": 0}})


def test_run_baseline_checks_every_case_before_recording_any(tmp_path):
    store = RecordingStore(tmp_path)
    one, two = independence("https://a.example/x", "https://b.test/y", case_id="c0"), \
        independence("https://a.example/x", "https://b.test/y", case_id="c1")
    b.run_baseline([one, two], store, "r1")
    changed = two.model_copy(update={"inputs": two.inputs.model_copy(
        update={"source_b_url": "https://c.test/y"})})
    fresh = independence("https://d.example/x", "https://e.test/y", case_id="c-new")
    with pytest.raises(Exception, match="c1"):
        b.run_baseline([fresh, one, changed], store, "r1")
    assert store.get(b.CANDIDATE, "c-new", "r1") is None  # nothing was half-written


def test_the_model_search_and_fetch_slots_are_tripwires():
    repos = MemoryRepos()
    deps = b._deps(repos, SourceStore(repos.sources, repos.observations, repos.passages))
    for slot in ("light_model", "writer_model", "search", "fetcher"):
        with pytest.raises(AssertionError, match="must not use"):
            getattr(deps, slot).anything


# ---- recording -------------------------------------------------------------------------------

def test_run_baseline_records_verdicts_that_replay_identically(tmp_path):
    cases = [pair(fields(), fields(value="4,270,000"), case_id="c0"),
             independence("https://a.example/x", "https://b.test/y", case_id="c1")]
    first = b.run_baseline(cases, RecordingStore(tmp_path), "r1")
    again = b.run_baseline(cases, RecordingStore(tmp_path), "r1")  # identical: a no-op
    assert first == again
    record = RecordingStore(tmp_path).get(b.CANDIDATE, "c0", "r1")
    assert record.config["similarity_threshold"] == 0.85 and record.config_dropped == []
