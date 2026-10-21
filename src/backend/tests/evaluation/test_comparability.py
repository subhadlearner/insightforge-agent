"""When two providers' figures for a metric can be compared (T16 #17): only when both ran under
the same conditions and services, and the metric has a defined value for the same briefs. A
metric object whose denominator is zero (every sampled Claim unchecked, no Claims) is not a
measurement."""

from insightforge_agent.evaluation.judge import not_judged
from insightforge_agent.evaluation.metrics import CitationMetrics, FactCheckMetrics
from insightforge_agent.evaluation.report import (
    LibraryInfo,
    ProviderResult,
    SampleResult,
    comparability_of,
    remediation_of,
    totals_of,
)

IDS = ["a", "b", "c", "d", "e"]
FULL = LibraryInfo(directory="samples/briefs", briefs_in_library=5, sample_ids=IDS,
                   fingerprint="f")
CONDITIONS = {"fact_check_rate": 0.2, "credibility_high": 0.65}
SERVICES = {"embedder": "FastEmbedEmbedder:BAAI/bge-small-en-v1.5", "search": "FallbackSearch",
            "fetcher": "HttpFetcher"}


def sample(sid: str, verified=5, unverified=0, unchecked=0, claims=5, accurate=5) -> SampleResult:
    return SampleResult(
        sample_id=sid, industry="i", question_type="q", status="complete", owner_id=f"eval-{sid}",
        fact_check=FactCheckMetrics(claims=claims, sampled=verified + unverified + unchecked,
                                    verified=verified, unverified=unverified,
                                    unchecked=unchecked),
        citations=CitationMetrics(claims=claims, accurate=accurate),
        synthesis=not_judged("none"))


def result(name: str, samples, conditions=CONDITIONS, services=SERVICES) -> ProviderResult:
    totals = totals_of(samples)
    return ProviderResult(provider=name, configured_models={}, runtime_models={},
                          runtime_services=services, conditions=conditions, workdir="w",
                          samples=samples, totals=totals,
                          remediation=remediation_of(name, samples, totals, FULL))


def test_a_brief_whose_sampled_claims_were_all_unchecked_is_not_a_fact_check_measurement():
    gemini = result("gemini", [sample("a", verified=0, unchecked=5)])
    groq = result("groq", [sample("a")])
    c = comparability_of([gemini, groq])
    assert c["fact_check"].comparable is False
    assert "gemini has none for a" in c["fact_check"].reason
    assert c["citations"].comparable is True  # a fact-check gap does not void citations


def test_a_report_with_no_claims_is_not_a_citation_measurement():
    gemini = result("gemini", [sample("a", claims=0, accurate=0, verified=0)])
    groq = result("groq", [sample("a")])
    assert comparability_of([gemini, groq])["citations"].comparable is False


def test_both_providers_with_nothing_checkable_are_not_comparable():
    gemini = result("gemini", [sample("a", verified=0, unchecked=5)])
    groq = result("groq", [sample("a", verified=0, unchecked=5)])
    c = comparability_of([gemini, groq])["fact_check"]
    assert c.comparable is False and "no provider was measured" in c.reason


def test_different_runtime_services_are_never_comparable():
    gemini = result("gemini", [sample("a")])
    groq = result("groq", [sample("a")], services={**SERVICES, "search": "OtherSearch"})
    for metric, c in comparability_of([gemini, groq]).items():
        assert c.comparable is False and "search" in c.reason, metric


def test_different_evidence_thresholds_are_never_comparable():
    gemini = result("gemini", [sample("a")])
    groq = result("groq", [sample("a")], conditions={**CONDITIONS, "credibility_high": 0.9})
    for metric, c in comparability_of([gemini, groq]).items():
        assert c.comparable is False and "credibility_high" in c.reason, metric


def test_the_same_conditions_services_and_measured_briefs_are_comparable():
    c = comparability_of([result("gemini", [sample("a")]), result("groq", [sample("a")])])
    assert c["fact_check"].comparable and c["citations"].comparable


def test_a_full_anthropic_run_with_one_brief_nothing_checkable_is_undetermined():
    """All five Runs complete; four pool above 80%; one checked nothing."""
    samples = [sample(s) for s in IDS[:4]] + [sample("e", verified=0, unchecked=5)]
    r = remediation_of("anthropic", samples, totals_of(samples), FULL)
    assert r.status == "undetermined" and "not measured: e" in r.reason



def test_a_t18_remediation_run_is_never_comparable_with_the_baseline():
    """T18 G3: the remediation behaviours are conditions, so a run under them is reported beside
    the baseline, never pooled with it."""
    baseline = result("baseline", [sample("a")], conditions={**CONDITIONS, "fact_check_method": "f2",
                                                             "writer_prompt_version": "writer-1"})
    remediated = result("remediated", [sample("a")], conditions={
        **CONDITIONS, "fact_check_method": "f2+counterpart-v1", "writer_prompt_version": "writer-2"})
    for metric, c in comparability_of([baseline, remediated]).items():
        assert c.comparable is False and "fact_check_method" in c.reason, metric
