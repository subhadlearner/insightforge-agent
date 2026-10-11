"""F2 (audit 2026-10-11): Fact-Checker accuracy with the production embedder (bge-small).

Each Claim goes through the real `fact_check()` against a re-fetched web page of one paragraph
(one Passage), with `FastEmbedEmbedder("BAAI/bge-small-en-v1.5")`, the approved threshold (0.85)
and the numeric check. Failure messages show the similarity, so a model or policy change is easy
to read. Results and the open decision: docs/audits/2026-10-11-f2-fact-checker.md.

- Plain tests: behaviour that holds today and must keep holding.
- `xfail(strict=True)`: known F2 failures under the approved policy. A fix that makes one pass
  fails the suite until its marker is removed."""

import pytest

from insightforge_agent.pipeline.fact_check import fact_check
from tests.pipeline.test_t6_writer_fact_check import URL, Fetcher, draft_of, evidence

SALES = "BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024."
PARAGRAPH = (f"{SALES} Plug-in hybrids made up about half of those sales, the company said in "
             "its annual filing. The company expanded its overseas network to 70 countries and "
             "opened plants in Hungary and Brazil.")
PARAPHRASE = "In 2025, BYD's sales reached 4.27 million vehicles, compared with 3.02 million in 2024."
EXPORTED = "BYD exported 4.27 million vehicles in 2025, up from 3.02 million in 2024."
TESLA = "Tesla sold 4.27 million vehicles in 2025, up from 3.02 million in 2024."
WRONG_YEAR = "BYD sold 4.27 million vehicles in 2024, up from 3.02 million in 2023."
F2_OPEN = "F2: needs an owner decision; see docs/audits/2026-10-11-f2-fact-checker.md"


def verdict_of(make_deps, embedder, claim: str, page: str):
    """The verdict for one Claim whose cited Evidence item and re-fetched page are `page`."""
    deps = make_deps(fetcher=Fetcher({URL: page}), embedder=embedder)
    _, bundle = evidence(deps, [page])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
    assert v.check == "web_refetch"
    assert len(deps.repos.passages.list("alice", v.matched.observation_id)) == 1, "one Passage"
    return v


def explain(v, claim: str, page: str) -> str:
    return (f"verdict={v.verdict} similarity={v.similarity:.3f} (threshold 0.85) "
            f"reason={v.reason!r}\n  claim:   {claim}\n  passage: {page}")


def assert_verdict(make_deps, embedder, claim: str, page: str, expected: str):
    v = verdict_of(make_deps, embedder, claim, page)
    assert v.verdict == expected, explain(v, claim, page)


# --- correct Claims must be verified ---------------------------------------------------------

@pytest.mark.parametrize("claim,page", [
    pytest.param("Tessera Energy's battery output reached 4.27 gigawatt-hours.",
                 "Battery output hit 4.27 GWh at Tessera Energy's Nevada plant.",
                 id="g-qu-02-abbreviation"),
    pytest.param(PARAPHRASE, SALES, id="writer-paraphrase-one-sentence-passage"),
])
def test_a_correct_claim_is_verified(make_deps, embedder, claim, page):
    assert_verdict(make_deps, embedder, claim, page, "verified")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=F2_OPEN)
@pytest.mark.parametrize("claim,page", [
    pytest.param(SALES, PARAGRAPH, id="verbatim-sentence-in-paragraph"),
    pytest.param(PARAPHRASE, PARAGRAPH, id="writer-paraphrase-in-paragraph"),
    pytest.param("BYD sold 4.27 million vehicles in 2025.", PARAGRAPH,
                 id="light-paraphrase-in-paragraph"),
])
def test_a_correct_claim_inside_a_longer_paragraph_is_verified(make_deps, embedder, claim, page):
    """Fails today: the Claim is one sentence of a three-sentence Passage, which dilutes it."""
    assert_verdict(make_deps, embedder, claim, page, "verified")


# --- unsupported Claims must not be verified -------------------------------------------------

@pytest.mark.parametrize("claim,page", [
    pytest.param(WRONG_YEAR, SALES, id="wrong-year-one-sentence-passage"),
    pytest.param(WRONG_YEAR, PARAGRAPH, id="wrong-year-in-paragraph"),
    # These two are rejected only because the paragraph dilutes the similarity; the same
    # edits against a one-sentence Passage are verified (the xfails below).
    pytest.param(EXPORTED, PARAGRAPH, id="wrong-measure-in-paragraph"),
    pytest.param(TESLA, PARAGRAPH, id="wrong-entity-in-paragraph"),
])
def test_an_unsupported_claim_is_not_verified(make_deps, embedder, claim, page):
    assert_verdict(make_deps, embedder, claim, page, "unverified")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=F2_OPEN)
@pytest.mark.parametrize("claim,page", [
    pytest.param("Orrin Bio's R&D spend was $90 million.",
                 "Orrin Bio's revenue was $90 million; its R&D spend was $40 million.",
                 id="g-ma-02-wrong-measure-right-number"),
    pytest.param(EXPORTED, SALES, id="wrong-measure-one-sentence-passage"),
    pytest.param(TESLA, SALES, id="wrong-entity-one-sentence-passage"),
])
def test_a_wrong_measure_or_entity_with_the_right_numbers_is_not_verified(make_deps, embedder,
                                                                          claim, page):
    """Fails today: similarity is high and every figure is in the Passage, so nothing rejects it."""
    assert_verdict(make_deps, embedder, claim, page, "unverified")
