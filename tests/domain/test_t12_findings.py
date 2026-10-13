"""T12 CP1: controlled vocabulary, Finding identity and normalisation (scripted judge only)."""

import json
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.agents.finding_judge import FindingJudge
from insightforge_agent.domain.findings import (
    DEFAULT_REGISTRY,
    Finding,
    JudgeRequest,
    PredicateRegistry,
    PredicateSpec,
    Skipped,
    normalize_seed,
    same_object,
)
from insightforge_agent.pipeline.findings import NormalizationFailed, normalize_seeds
from tests.domain.t12_builders import finding, seed
from tests.scripted import ScriptedChat
from tests.stores.t12_world import unsupported_reply


def judge_replying(*judgments, raw=None):
    text = raw if raw is not None else json.dumps({"judgments": list(judgments)})
    return ScriptedChat(reply=lambda messages: AIMessage(content=text))


# --- vocabulary ---------------------------------------------------------------------------------

def test_aliases_resolve_to_one_predicate_and_declared_polarity():
    assert DEFAULT_REGISTRY.resolve("Starting Price")[0].name == "price"
    assert DEFAULT_REGISTRY.resolve("starting-price")[0].name == "price"
    spec, polarity = DEFAULT_REGISTRY.resolve("discontinued")
    assert (spec.name, polarity) == ("offers", "negated")
    assert DEFAULT_REGISTRY.resolve("vibes") is None
    assert DEFAULT_REGISTRY.resolve("") is None


def test_vocabulary_rejects_ambiguous_aliases_and_undeclared_tolerances():
    a = PredicateSpec(name="a", cardinality="single", object_type="text", aliases={"x"})
    b = PredicateSpec(name="b", cardinality="single", object_type="text", aliases={"x"})
    with pytest.raises(ValueError, match="declared twice"):
        PredicateRegistry([a, b])
    with pytest.raises(ValueError, match="only a quantity"):
        PredicateSpec(name="c", cardinality="single", object_type="entity", tolerance=1)
    with pytest.raises(ValueError):
        PredicateSpec(name="d", cardinality="single", object_type="quantity", tolerance=-1)


def test_default_tolerance_is_zero_after_canonicalisation():
    price = DEFAULT_REGISTRY.get("price")
    assert all(s.tolerance == 0 for s in map(DEFAULT_REGISTRY.get, DEFAULT_REGISTRY.names()))
    a, b = finding(value="$1,000"), finding(value="1 thousand dollars")
    assert a.object_key == b.object_key
    assert not same_object(price, finding(value="$100").object_key,
                           finding(value="$100.01").object_key)


def test_declared_tolerance_is_applied_in_the_canonical_unit_only():
    spec = PredicateSpec(name="share", cardinality="single", object_type="quantity",
                         tolerance=Decimal("0.5"))
    reg = PredicateRegistry([spec])
    a = finding(predicate="share", value="20%", registry=reg)
    b = finding(predicate="share", value="20.5%", registry=reg)
    c = finding(predicate="share", value="20.6%", registry=reg)
    d = finding(predicate="share", value="20.4 points", registry=reg)
    assert same_object(spec, a.object_key, b.object_key)
    assert not same_object(spec, a.object_key, c.object_key)
    assert not same_object(spec, a.object_key, d.object_key)


# --- identity -----------------------------------------------------------------------------------

def test_single_valued_identity_ignores_object_and_as_of_period():
    a = finding(value="$100", period="2029")
    b = finding(value="$130", period="2030-02", entity="  ACME ")
    assert a.key == b.key
    assert finding(scope="EU").key != a.key


def test_multi_valued_identity_includes_object_but_not_period():
    a = finding(predicate="offers", value="Model X", period="2029")
    b = finding(predicate="offers", value="model x", period="2030")
    c = finding(predicate="offers", value="Model Y")
    assert a.key == b.key != c.key


# --- deterministic normalisation ----------------------------------------------------------------

def test_in_vocabulary_complete_triple_needs_no_judge_and_keeps_provenance():
    s = seed(derived_from="observation_date", period="2030-03-01")
    f = normalize_seed(s, 0, "r1", DEFAULT_REGISTRY)
    assert isinstance(f, Finding)
    assert (f.subject, f.predicate, f.object, f.polarity) == ("Acme", "price", "$100", "affirmed")
    assert f.derived and f.as_of_period == "2030-03-01"
    p = f.provenance
    assert (p.run_id, p.report_id, p.evidence_id) == ("r1", "rep1", "e1")
    assert p.primary == s.evidence.primary and p.source_ids == ["src1"]
    assert p.observation_dates == s.observation_dates


def test_negated_alias_is_a_negated_finding_without_a_judge():
    f = normalize_seed(seed(predicate="discontinued", value="Model X"), 0, "r1",
                       DEFAULT_REGISTRY)
    assert isinstance(f, Finding) and (f.predicate, f.polarity) == ("offers", "negated")


@pytest.mark.parametrize("kw, reason", [
    ({"historical": True}, "Historical"),
    ({"run_id": "elsewhere"}, "another Run"),
])
def test_historical_and_foreign_run_claims_never_become_findings(kw, reason):
    result = normalize_seed(seed(**kw), 0, "r1", DEFAULT_REGISTRY)
    assert isinstance(result, Skipped) and reason in result.reason


def test_unparseable_quantity_is_skipped_not_compared_as_text():
    result = normalize_seed(seed(value="about $100"), 0, "r1", DEFAULT_REGISTRY)
    assert isinstance(result, Skipped) and "exact quantity" in result.reason


def test_unknown_or_ambiguous_period_is_kept_as_unknown():
    assert normalize_seed(seed(period="sometime"), 0, "r1", DEFAULT_REGISTRY).as_of_period \
        == "UNKNOWN"
    f = normalize_seed(seed(temporally_ambiguous=True, derived_from="observation_date"), 0,
                       "r1", DEFAULT_REGISTRY)
    assert f.as_of_period == "UNKNOWN" and not f.derived


@pytest.mark.parametrize("kw, reason", [
    ({"predicate": "vibes"}, "out_of_vocabulary"),
    ({"predicate": "", "value": ""}, "incomplete_triple"),
    ({"predicate": "offers", "value": "Model X"}, "polarity"),
])
def test_only_unsettled_claims_go_to_the_judge(kw, reason):
    result = normalize_seed(seed(**kw), 0, "r1", DEFAULT_REGISTRY)
    assert isinstance(result, JudgeRequest) and result.reason == reason


# --- the judge ----------------------------------------------------------------------------------

def test_judge_maps_out_of_vocabulary_claims_and_reads_polarity():
    seeds = [
        seed(predicate="", value="", entity="", evidence_id="e0",
             statement="Acme's chief executive is Jane Roe."),
        seed(predicate="offers", value="Model X", evidence_id="e1",
             statement="Acme no longer offers Model X."),
        seed(evidence_id="e2"),  # settled by the vocabulary
    ]
    model = judge_replying(
        {"index": 0, "predicate": "ceo", "polarity": "affirmed", "subject": "Acme",
         "object": "Jane Roe"},
        {"index": 1, "predicate": "offers", "polarity": "negated", "subject": "Acme",
         "object": "Model X"},
    )
    out = normalize_seeds(seeds, "r1", DEFAULT_REGISTRY, FindingJudge(model, DEFAULT_REGISTRY))
    by_evidence = {f.provenance.evidence_id: f for f in out.findings}
    assert (by_evidence["e0"].predicate, by_evidence["e0"].object) == ("ceo", "Jane Roe")
    assert by_evidence["e0"].resolution == "judge"
    # Polarity-only request: the judge echoes the fixed triple and reads its polarity.
    assert (by_evidence["e1"].predicate, by_evidence["e1"].object, by_evidence["e1"].polarity) \
        == ("offers", "Model X", "negated")
    assert by_evidence["e2"].resolution == "vocabulary"
    assert len(model.seen) == 1  # one bounded call; Passage text is never sent
    sent = model.seen[0][-1].content
    assert "Jane Roe" in sent and "e2" not in sent
    assert json.loads(sent)[1]["fixed_triple"] == {"predicate": "offers", "subject": "Acme",
                                                   "object": "Model X"}


def test_an_unsupported_answer_skips_the_claim():
    model = judge_replying({"index": 0, "predicate": "unsupported", "polarity": "affirmed"})
    out = normalize_seeds([seed(predicate="vibes"), seed(evidence_id="e2")], "r1",
                          DEFAULT_REGISTRY, FindingJudge(model, DEFAULT_REGISTRY))
    assert [f.provenance.evidence_id for f in out.findings] == ["e2"]
    assert "no controlled predicate" in out.skipped[0].reason


@pytest.mark.parametrize("judgment, reason", [
    ({"index": 0, "predicate": "mood", "polarity": "affirmed", "subject": "Acme",
      "object": "$100"}, "unknown predicate"),
    ({"index": 0, "predicate": "price", "polarity": "affirmed", "subject": "Globex",
      "object": "$100"}, "not stated by the Claim"),
    ({"index": 9, "predicate": "price", "polarity": "affirmed", "subject": "Acme",
      "object": "$100"}, "no judgment"),  # an answer for a Claim that was never asked
])
def test_an_answer_failing_the_checks_fails_normalisation(judgment, reason):
    model = judge_replying(judgment)
    with pytest.raises(NormalizationFailed, match=reason):
        normalize_seeds([seed(predicate="vibes", statement="Acme price is $100.")], "r1",
                        DEFAULT_REGISTRY, FindingJudge(model, DEFAULT_REGISTRY))


@pytest.mark.parametrize("raw", ["not json", '{"judgments": [{"index": "x"}]}', ""])
def test_a_malformed_reply_fails_normalisation_and_never_guesses(raw):
    model = judge_replying(raw=raw)
    with pytest.raises(NormalizationFailed, match="reply was invalid"):
        normalize_seeds([seed(predicate="vibes"), seed(evidence_id="e2")], "r1",
                        DEFAULT_REGISTRY, FindingJudge(model, DEFAULT_REGISTRY))


def test_a_missing_judgment_fails_normalisation():
    with pytest.raises(NormalizationFailed, match="e1: no judgment"):
        normalize_seeds([seed(predicate="offers", value="Model X")], "r1", DEFAULT_REGISTRY,
                        FindingJudge(judge_replying(), DEFAULT_REGISTRY))


def test_no_judge_fails_normalisation_only_when_one_is_needed():
    assert normalize_seeds([seed()], "r1", DEFAULT_REGISTRY, None).findings
    with pytest.raises(NormalizationFailed, match="none is configured"):
        normalize_seeds([seed(predicate="vibes")], "r1", DEFAULT_REGISTRY, None)


def test_judge_transport_errors_propagate_for_retry():
    def boom(messages):
        raise ConnectionError("provider down")
    with pytest.raises(ConnectionError):
        normalize_seeds([seed(predicate="vibes")], "r1", DEFAULT_REGISTRY,
                        FindingJudge(ScriptedChat(reply=boom), DEFAULT_REGISTRY))


def test_judge_calls_are_batched():
    seeds = [seed(predicate="vibes", evidence_id=f"e{i}") for i in range(45)]
    model = ScriptedChat(reply=unsupported_reply)
    out = normalize_seeds(seeds, "r1", DEFAULT_REGISTRY, FindingJudge(model, DEFAULT_REGISTRY))
    assert len(model.seen) == 3 and len(out.skipped) == 45


@pytest.mark.parametrize("judgment", [
    {"index": 0, "predicate": "unsupported", "polarity": "affirmed"},  # empty subject/object
    {"index": 0, "predicate": "competes_with", "polarity": "negated", "subject": "Acme",
     "object": "Model X"},
    {"index": 0, "predicate": "offers", "polarity": "negated", "subject": "Acme",
     "object": "Model Z"},
    {"index": 0, "predicate": "offers", "polarity": "negated", "subject": "", "object": ""},
])
def test_a_polarity_answer_must_repeat_the_fixed_triple(judgment):
    with pytest.raises(NormalizationFailed, match="must repeat the fixed predicate"):
        normalize_seeds([seed(predicate="sells", value="Model X")], "r1", DEFAULT_REGISTRY,
                        FindingJudge(judge_replying(judgment), DEFAULT_REGISTRY))


def test_a_valid_polarity_answer_tolerates_case_and_spacing():
    out = normalize_seeds([seed(predicate="sells", value="Model X")], "r1", DEFAULT_REGISTRY,
                          FindingJudge(judge_replying(
                              {"index": 0, "predicate": "offers", "polarity": "negated",
                               "subject": " acme", "object": "model  x"}), DEFAULT_REGISTRY))
    [f] = out.findings
    assert (f.predicate, f.object, f.polarity) == ("offers", "Model X", "negated")
