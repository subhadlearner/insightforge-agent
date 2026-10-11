"""F2 (audit 2026-10-11): Fact-Checker accuracy regressions, through the real `fact_check()`.

Each Claim is checked against a re-fetched web page that is one paragraph (one Passage), with the
production embedder and the approved threshold (0.85) and numeric check.

- `xfail(strict=True)`: the known F2 failures. Each must flip to passing when F2 is fixed, and
  then the marker has to be removed (strict makes an unexpected pass fail the suite).
- Plain tests: guards. Unsupported Claims that are rejected today must stay rejected, so a fix
  cannot raise the pass rate by accepting them (see docs/audits/2026-10-11-f2-fact-checker.md:
  sentence-level matching with the hashing embedder would verify all three)."""

import pytest

from tests.pipeline.test_t6_writer_fact_check import URL, Fetcher, draft_of, evidence
from insightforge_agent.pipeline.fact_check import fact_check

PARAGRAPH = ("BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024. Plug-in hybrids "
             "made up about half of those sales, the company said in its annual filing. The company "
             "expanded its overseas network to 70 countries and opened plants in Hungary and Brazil.")
F2_PENDING = "F2: needs semantic matching; see docs/audits/2026-10-11-f2-fact-checker.md"


def verdict_of(make_deps, claim: str, page: str):
    """The verdict for one Claim whose cited Evidence item and re-fetched page are `page`."""
    deps = make_deps(fetcher=Fetcher({URL: page}))
    _, bundle = evidence(deps, [page])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
    assert v.check == "web_refetch"
    return v


# --- known failures: correct Claims rejected, a wrong-measure Claim accepted ----------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason=F2_PENDING)
@pytest.mark.parametrize("claim,page", [
    pytest.param("Tessera Energy's battery output reached 4.27 gigawatt-hours.",
                 "Battery output hit 4.27 GWh at Tessera Energy's Nevada plant.",
                 id="g-qu-02-abbreviation"),
    pytest.param("BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024.", PARAGRAPH,
                 id="verbatim-sentence-in-paragraph"),
    pytest.param("In 2025, BYD's sales reached 4.27 million vehicles, compared with 3.02 million in 2024.",
                 PARAGRAPH, id="writer-paraphrase"),
])
def test_a_correct_claim_is_verified(make_deps, claim, page):
    assert verdict_of(make_deps, claim, page).verdict == "verified"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=F2_PENDING)
def test_a_wrong_measure_claim_with_the_right_number_is_rejected(make_deps):
    """g-ma-02: $90 million is the revenue; the R&D spend is $40 million."""
    v = verdict_of(make_deps, "Orrin Bio's R&D spend was $90 million.",
                   "Orrin Bio's revenue was $90 million; its R&D spend was $40 million.")
    assert v.verdict == "unverified"


# --- guards: unsupported Claims rejected today must stay rejected ----------------------------

@pytest.mark.parametrize("claim", [
    pytest.param("BYD exported 4.27 million vehicles in 2025, up from 3.02 million in 2024.",
                 id="wrong-measure-own-sentence"),
    pytest.param("Tesla sold 4.27 million vehicles in 2025, up from 3.02 million in 2024.",
                 id="wrong-entity"),
    pytest.param("BYD sold 4.27 million vehicles in 2024, up from 3.02 million in 2023.",
                 id="wrong-year"),
])
def test_an_unsupported_claim_is_not_verified(make_deps, claim):
    assert verdict_of(make_deps, claim, PARAGRAPH).verdict == "unverified"
