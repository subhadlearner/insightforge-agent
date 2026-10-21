"""F2 (audit 2026-10-11): Fact-Checker verification with the production embedder (bge-small).

Each Claim goes through the real `fact_check()` against a re-fetched web page of one paragraph
(one Passage): real `FastEmbedEmbedder("BAAI/bge-small-en-v1.5")` retrieval over sentence spans,
the approved threshold (0.85), the numeric gate, then one entailment call. Every case prints its
spans, scores and verdict (`-rP`). Results: docs/audits/2026-10-11-f2-fact-checker.md.

What is real and what is scripted:
- Real: span generation, bge-small similarity, the 0.85 gate, the numeric gate, which spans
  reach the entailment call, and the verdict plumbing.
- Scripted (no paid call): the entailment judge. It is an oracle: it answers SUPPORTED only for
  a span containing the sentence that states the Claim (`support`), else INSUFFICIENT_EVIDENCE.
  So a correct Claim verifies only if bge-small retrieved that sentence. Whether a real light
  model judges these spans as the oracle does is a separate, owner-authorised live check.
- `reaches_judge` pins the measured division of labour: bge-small passes wrong-measure and
  wrong-entity Claims to the judge (it cannot reject them), while the numeric gate stops
  wrong-year Claims before any model call."""

import json

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.pipeline.fact_check import fact_check, screen
from tests.pipeline.test_t6_writer_fact_check import SALES, URL, Fetcher, draft_of, evidence
from tests.scripted import ScriptedChat, support_verdicts

PARAGRAPH = (f"{SALES} Plug-in hybrids made up about half of those sales, the company said in "
             "its annual filing. The company expanded its overseas network to 70 countries and "
             "opened plants in Hungary and Brazil.")
PARAPHRASE = "In 2025, BYD's sales reached 4.27 million vehicles, compared with 3.02 million in 2024."
EXPORTED = "BYD exported 4.27 million vehicles in 2025, up from 3.02 million in 2024."
TESLA = "Tesla sold 4.27 million vehicles in 2025, up from 3.02 million in 2024."
WRONG_YEAR = "BYD sold 4.27 million vehicles in 2024, up from 3.02 million in 2023."
GMA_02 = "Orrin Bio's revenue was $90 million; its R&D spend was $40 million."
GM = "GM delivered 2.7 million vehicles in the US in 2025."
GENERAL_MOTORS = "General Motors delivered 2.7 million vehicles in the US in 2025."
GWH = "Battery output hit 4.27 GWh at Tessera Energy's Nevada plant."
PRONOUN = "BYD is a Chinese automaker. It sold 3.02 million vehicles in 2023."


def oracle(support: str | None) -> ScriptedChat:
    def reply(messages) -> AIMessage:
        return AIMessage(content=json.dumps({"verdicts": support_verdicts(
            str(messages[-1].content), lambda _, text: "SUPPORTED" if support and support in text
            else "INSUFFICIENT_EVIDENCE")}))
    return ScriptedChat(reply=reply)


def assert_verdict(make_deps, embedder, claim, page, support, expected, reaches_judge):
    judge = oracle(support)
    deps = make_deps(fetcher=Fetcher({URL: page}), embedder=embedder, light_model=judge)
    _, bundle = evidence(deps, [page])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
    if v.check != "web_refetch" or v.matched is None:
        raise RuntimeError(f"setup: expected a re-fetched Passage, got {v!r}")
    passages = deps.repos.passages.list("alice", v.matched.observation_id)
    if len(passages) != 1:
        raise RuntimeError(f"setup: expected one Passage, got {len(passages)}")
    gates = screen(deps, claim, passages)  # the same real gates, for the diagnostics
    shown = "\n".join(f"    {score:.3f}  {span.text}" for score, span in gates.candidates)
    explained = (f"verdict={v.verdict} similarity={v.similarity:.3f} "
                 f"(threshold {deps.similarity_threshold}) model calls={len(judge.seen)} "
                 f"reason={v.reason!r}\n  claim:   {claim}\n  passage: {page}\n"
                 f"  candidates judged:\n{shown or '    (none)'}")
    print(explained)
    assert bool(judge.seen) == reaches_judge, explained
    if support and expected == "verified":
        assert any(support in span.text for _, span in gates.candidates), explained
    assert v.verdict == expected, explained


CASES = [
    # claim, page, sentence that states it (None: nothing does), verdict, reaches the judge
    pytest.param("Tessera Energy's battery output reached 4.27 gigawatt-hours.", GWH, GWH,
                 "verified", True, id="g-qu-02-abbreviation"),
    pytest.param(PARAPHRASE, SALES, SALES, "verified", True,
                 id="writer-paraphrase-one-sentence-passage"),
    pytest.param(GENERAL_MOTORS, GM, GM, "verified", True, id="entity-full-name-vs-abbreviation"),
    pytest.param(GM, GENERAL_MOTORS, GENERAL_MOTORS, "verified", True,
                 id="entity-abbreviation-vs-full-name"),
    pytest.param(SALES, SALES.replace("BYD", "BYD Co. Ltd."), "BYD Co. Ltd. sold", "verified",
                 True, id="entity-legal-name"),
    pytest.param(SALES, PARAGRAPH, SALES, "verified", True, id="verbatim-sentence-in-paragraph"),
    pytest.param(PARAPHRASE, PARAGRAPH, SALES, "verified", True,
                 id="writer-paraphrase-in-paragraph"),
    pytest.param("BYD sold 4.27 million vehicles in 2025.", PARAGRAPH, SALES, "verified", True,
                 id="light-paraphrase-in-paragraph"),
    pytest.param("BYD sold 3.02 million vehicles in 2023.", PRONOUN, PRONOUN, "verified", True,
                 id="subject-in-previous-sentence"),
    pytest.param(WRONG_YEAR, SALES, None, "unverified", False,
                 id="wrong-year-one-sentence-passage"),
    pytest.param(WRONG_YEAR, PARAGRAPH, None, "unverified", False, id="wrong-year-in-paragraph"),
    pytest.param("Orrin Bio's R&D spend was $90 million.", GMA_02, None, "unverified", True,
                 id="g-ma-02-wrong-measure-right-number"),
    pytest.param(EXPORTED, SALES, None, "unverified", True,
                 id="wrong-measure-one-sentence-passage"),
    pytest.param(EXPORTED, PARAGRAPH, None, "unverified", True, id="wrong-measure-in-paragraph"),
    pytest.param(TESLA, SALES, None, "unverified", True, id="wrong-entity-one-sentence-passage"),
    pytest.param(TESLA, PARAGRAPH, None, "unverified", True, id="wrong-entity-in-paragraph"),
]


@pytest.mark.parametrize("claim,page,support,expected,reaches_judge", CASES)
def test_f2_regression(make_deps, embedder, claim, page, support, expected, reaches_judge):
    assert_verdict(make_deps, embedder, claim, page, support, expected, reaches_judge)
