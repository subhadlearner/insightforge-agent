"""F2: span matching, the deterministic gates and the one entailment call per sampled Claim.

The light model is scripted (no paid call) and records what it was shown; the embedder is the
deterministic `HashingEmbedder`, so the gates are exact. Real-embedder retrieval is covered by
tests/integration/test_f2_fact_check_bge.py."""

import json
import math
import re

import pytest
from langchain_core.messages import AIMessage

from insightforge_agent.domain.models import Passage
from insightforge_agent.domain.passages import candidate_spans
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.embeddings import HashingEmbedder, cosine
from insightforge_agent.pipeline.entailment import ENTAIL_SYSTEM
from insightforge_agent.pipeline.fact_check import TOP_K, fact_check, screen
from tests.pipeline.test_t6_writer_fact_check import SALES, URL, Fetcher, draft_of, evidence
from tests.scripted import ScriptedChat

PARAGRAPH = (f"{SALES} Plug-in hybrids made up about half of those sales. The company opened "
             "plants in Hungary and Brazil.")


def numbered(messages) -> list[tuple[int, str]]:
    return [(int(n), t) for n, t in re.findall(r"^\[(\d+)\] (.+)$", str(messages[-1].content),
                                               re.MULTILINE)]


def judge(*verdicts: str) -> ScriptedChat:
    """Answers every call with `verdicts` in order, one per numbered span (the last repeats)."""
    def reply(messages) -> AIMessage:
        spans = numbered(messages)
        out = [{"passage": n, "verdict": verdicts[min(i, len(verdicts) - 1)]}
               for i, (n, _) in enumerate(spans)]
        return AIMessage(content=json.dumps({"verdicts": out}))
    return ScriptedChat(reply=reply)


def replying(content: str) -> ScriptedChat:
    return ScriptedChat(reply=lambda messages: AIMessage(content=content))


def check(make_deps, model, claim=SALES, page=SALES, kind="web", **deps_overrides):
    deps = make_deps(fetcher=Fetcher({URL: page}), light_model=model, **deps_overrides)
    obs, bundle = evidence(deps, [page], kind=kind)
    result = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle)
    (v,) = result.verdicts
    return v, deps, result


def events(deps, type_):
    return [e.payload for e in deps.repos.events.read_after("alice", "run_1") if e.type == type_]


# --- no model call when the deterministic gates leave no span ---------------------------------

def test_no_entailment_call_when_no_span_clears_the_threshold(make_deps):
    """F2 alone (the T18 counterpart fallback off): the floor stops the call."""
    model = judge("SUPPORTED")
    v, deps, _ = check(make_deps, model, claim="Tesla opened a factory in Berlin.",
                       fact_check_counterpart=False)
    assert v.verdict == "unverified" and v.similarity < 0.85
    assert v.reason == "no Passage is similar enough to the Claim"
    assert model.seen == [] and events(deps, "fact_check_entailment") == []


def test_with_the_counterpart_fallback_the_unchanged_cited_passage_goes_to_the_judge(make_deps):
    """T18 CS1A, by design: below the floor, the Claim's own unchanged cited Passage is judged,
    and the judge, not the floor, rejects an unsupported Claim."""
    model = judge("INSUFFICIENT_EVIDENCE")
    v, deps, _ = check(make_deps, model, claim="Tesla opened a factory in Berlin.")
    assert v.verdict == "unverified" and len(model.seen) == 1
    assert "no candidate span was judged SUPPORTED" in v.reason
    (attempt,) = events(deps, "fact_check_counterpart")
    assert (attempt["entered_top_k"], attempt["verified_by_counterpart"]) == (True, False)


def test_no_entailment_call_when_the_numeric_guard_rejects_every_candidate(make_deps):
    model = judge("SUPPORTED")
    v, _, _ = check(make_deps, model, page=SALES.replace("4.27", "4.28"))
    assert v.verdict == "unverified" and v.similarity >= 0.85 and "4.27" in v.reason
    assert model.seen == []


# --- one bounded call -------------------------------------------------------------------------

def test_qualifying_spans_are_judged_in_one_call_bounded_to_top_k(make_deps):
    model = judge("SUPPORTED")
    v, _, _ = check(make_deps, model, page="\n\n".join([SALES] * 5))  # 5 identical Passages
    assert v.verdict == "verified"
    assert len(model.seen) == 1
    shown = numbered(model.seen[0])
    assert [n for n, _ in shown] == list(range(1, TOP_K + 1)) and TOP_K == 3
    assert all(text == SALES for _, text in shown)
    assert str(model.seen[0][-1].content).startswith(f"Statement: {SALES}\n\n")


def test_a_claim_inside_a_paragraph_is_judged_against_its_sentence_not_the_paragraph(make_deps):
    model = judge("SUPPORTED")
    v, deps, _ = check(make_deps, model, page=PARAGRAPH)
    texts = [t for _, t in numbered(model.seen[0])]
    # The sentence, widened to the window with its neighbour; never the whole paragraph.
    window = f"{SALES} Plug-in hybrids made up about half of those sales."
    assert texts == [window]
    a, b = deps.embedder.embed([SALES, window])
    assert v.verdict == "verified" and v.similarity == pytest.approx(cosine(a, b))
    assert v.similarity < 1.0  # the window's own score, not the exact sentence's


class TableEmbedder:
    """The Claim is [1, 0]; a listed span is at the given cosine from it; anything else is 0."""

    def __init__(self, claim: str, scores: dict[str, float]) -> None:
        self.claim, self.scores, self.embedded = claim, scores, []

    def embed(self, texts):
        self.embedded.extend(texts)
        return [[1.0, 0.0] if t == self.claim else
                [self.scores.get(t, 0.0), math.sqrt(1 - self.scores.get(t, 0.0) ** 2)]
                for t in texts]


def test_a_sentence_and_its_own_windows_cannot_crowd_out_a_supporting_sentence(make_deps):
    claim = "First part. Second part. Third part."  # three sentences: windows up to three wide
    near = Passage(observation_id="o", index=0, text="Near miss. Tail one. Tail two.")
    good = Passage(observation_id="o", index=1, text="Supporting sentence.")
    deps = make_deps(embedder=TableEmbedder(claim, {
        "Near miss.": 0.96, "Near miss. Tail one.": 0.955, "Near miss. Tail one. Tail two.": 0.95,
        "Supporting sentence.": 0.93}))
    s = screen(deps, claim, [near, good])
    # The near miss and its windows share one place (widened to the window that passes the
    # gates, with that window's own score); the supporting sentence keeps its own.
    assert [(score, span.text) for score, span in s.candidates] == [
        (pytest.approx(0.95), "Near miss. Tail one. Tail two."),
        (pytest.approx(0.93), "Supporting sentence.")]
    assert s.candidates[1][1].passage == good


def test_a_sentence_without_its_subject_is_widened_to_the_window_that_names_it(make_deps):
    claim = "BYD sold 3.02 million vehicles in 2023."
    page = Passage(observation_id="o", index=0,
                   text="BYD is a Chinese automaker. It sold 3.02 million vehicles in 2023.")
    deps = make_deps(embedder=TableEmbedder(claim, {
        "It sold 3.02 million vehicles in 2023.": 0.95, page.text: 0.90}))
    s = screen(deps, claim, [page])
    # The widened span carries its own similarity (0.90), not the sentence's (0.95).
    assert [(score, span.text) for score, span in s.candidates] == [
        (pytest.approx(0.90), page.text)]
    assert s.similarity == pytest.approx(0.90)


def test_a_verified_widened_span_reports_its_own_similarity(make_deps):
    claim = "BYD sold 3.02 million vehicles in 2023."
    page = "BYD is a Chinese automaker. It sold 3.02 million vehicles in 2023."
    model = judge("SUPPORTED")
    deps = make_deps(fetcher=Fetcher({URL: page}), light_model=model,
                     embedder=TableEmbedder(claim, {
                         "It sold 3.02 million vehicles in 2023.": 0.95, page: 0.90}))
    _, bundle = evidence(deps, [page])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
    assert v.verdict == "verified" and v.similarity == pytest.approx(0.90)
    assert [t for _, t in numbered(model.seen[0])] == [page]


def test_a_window_that_only_partly_covers_another_group_does_not_widen(make_deps):
    claim = "Claim sentence."
    page = Passage(observation_id="o", index=0, text="S one. S two. S three.")
    deps = make_deps(embedder=TableEmbedder(claim, {
        "S one.": 0.97, "S two. S three.": 0.96, "S one. S two.": 0.95}))
    s = screen(deps, claim, [page])
    # Widening "S one." to "S one. S two." would show "S two." twice.
    assert [span.text for _, span in s.candidates] == ["S one.", "S two. S three."]


ANCHOR_SCORES = {"Anchor.": 0.99, "Anchor. Context.": 0.86,
                 "Bee.": 0.95, "Cee.": 0.93, "Dee.": 0.91}
ANCHOR_PAGE = "Anchor. Context.\n\nBee.\n\nCee.\n\nDee."  # four Passages


def test_a_widened_group_keeps_the_rank_of_its_best_sentence(make_deps):
    claim = "Claim sentence."
    passages = [Passage(observation_id="o", index=i, text=t)
                for i, t in enumerate(ANCHOR_PAGE.split("\n\n"))]
    deps = make_deps(embedder=TableEmbedder(claim, ANCHOR_SCORES))
    s = screen(deps, claim, passages)
    # Ranked by 0.99 (the anchor), judged as the window, reported at the window's own 0.86.
    assert [(score, span.text) for score, span in s.candidates] == [
        (pytest.approx(0.86), "Anchor. Context."),
        (pytest.approx(0.95), "Bee."), (pytest.approx(0.93), "Cee.")]
    assert s.similarity == pytest.approx(0.86) and s.matched.index == 0
    assert all(score == pytest.approx(ANCHOR_SCORES[span.text]) for score, span in s.candidates)


def test_the_judge_sees_the_widened_window_and_the_verdict_reports_its_similarity(make_deps):
    claim = "Claim sentence."
    model = judge("SUPPORTED", "INSUFFICIENT_EVIDENCE")
    deps = make_deps(fetcher=Fetcher({URL: ANCHOR_PAGE}), light_model=model,
                     embedder=TableEmbedder(claim, ANCHOR_SCORES))
    _, bundle = evidence(deps, [ANCHOR_PAGE])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle).verdicts
    assert [t for _, t in numbered(model.seen[0])] == ["Anchor. Context.", "Bee.", "Cee."]
    assert v.verdict == "verified" and v.similarity == pytest.approx(0.86)  # not 0.99
    assert v.matched.index == 0


def test_a_window_below_the_threshold_is_not_made_eligible_by_its_anchor(make_deps):
    claim = "Claim sentence."
    page = Passage(observation_id="o", index=0, text="Anchor. Context.")
    deps = make_deps(embedder=TableEmbedder(claim, {"Anchor.": 0.99, "Anchor. Context.": 0.80}))
    s = screen(deps, claim, [page])
    assert [(score, span.text) for score, span in s.candidates] == [
        (pytest.approx(0.99), "Anchor.")]


def test_a_window_covering_two_chosen_spans_takes_one_place(make_deps):
    claim = "Sales rose. Costs held. Prices fell."  # three sentences: three-sentence windows
    page = Passage(observation_id="o", index=0, text="Sales rose. Margins held. Prices fell.")
    deps = make_deps(embedder=TableEmbedder(claim, {
        "Sales rose.": 0.97, "Prices fell.": 0.96, page.text: 0.90}))
    s = screen(deps, claim, [page])
    assert [span.text for _, span in s.candidates] == [page.text]


def request_tokens(messages) -> int:
    return sum(estimate_tokens(str(m.content)) for m in messages)


def capped(make_deps, model, cap: int):
    page = "\n\n".join([SALES] * 5)  # five identical Passages: three candidates qualify
    deps = make_deps(fetcher=Fetcher({URL: page}), light_model=model, passage_token_cap=cap)
    _, bundle = evidence(deps, [page])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle).verdicts
    return v, deps


def test_weaker_candidates_are_dropped_until_the_request_fits_the_cap(make_deps):
    one = estimate_tokens(ENTAIL_SYSTEM) + estimate_tokens(f"Statement: {SALES}\n\n[1] {SALES}")
    model = judge("SUPPORTED")
    v, _ = capped(make_deps, model, cap=one)
    assert v.verdict == "verified" and len(numbered(model.seen[0])) == 1
    assert request_tokens(model.seen[0]) <= one


def test_no_call_and_unverified_when_even_the_best_candidate_exceeds_the_cap(make_deps):
    model = judge("SUPPORTED")
    v, deps = capped(make_deps, model, cap=1)
    assert model.seen == []
    assert v.verdict == "unverified" and "exceeds the per-call cap of 1" in v.reason
    (skipped,) = events(deps, "fact_check_entailment_skipped")
    assert skipped["passage_token_cap"] == 1 and skipped["tokens"] > 1


def test_every_request_at_the_default_cap_fits_it(make_deps):
    model = judge("SUPPORTED")
    v, deps, _ = check(make_deps, model, page="\n\n".join([PARAGRAPH] * 4))
    assert v.verdict == "verified" and len(numbered(model.seen[0])) == TOP_K
    assert request_tokens(model.seen[0]) <= deps.passage_token_cap


def test_an_assertion_error_from_the_model_fails_closed_like_any_exception(make_deps):
    def broken(messages):
        raise AssertionError("library invariant failed")
    v, deps, result = check(make_deps, ScriptedChat(reply=broken))
    assert v.verdict == "unverified" and "AssertionError" in v.reason
    assert result.summary["unchecked"] == 0 and result.summary["pass_rate"] == 0.0
    (failed,) = events(deps, "fact_check_entailment_failed")
    assert "library invariant failed" in failed["error"]


def test_an_interrupt_is_not_caught(make_deps):
    def interrupted(messages):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        check(make_deps, ScriptedChat(reply=interrupted))


def test_a_source_cited_by_several_claims_has_its_spans_embedded_once(make_deps):
    class CountingEmbedder(HashingEmbedder):
        def __init__(self) -> None:
            super().__init__()
            self.embedded: list[str] = []

        def embed(self, texts):
            self.embedded.extend(texts)
            return super().embed(texts)

    other = "Plug-in hybrids made up about half of those sales."
    counting = CountingEmbedder()
    deps = make_deps(fetcher=Fetcher({URL: PARAGRAPH}), embedder=counting,
                     light_model=judge("SUPPORTED"))
    _, bundle = evidence(deps, [SALES, other], body=PARAGRAPH)
    result = fact_check(deps, "alice", "run_1", draft_of([SALES, other]), bundle)
    assert [v.verdict for v in result.verdicts] == ["verified", "verified"]
    spans = [s.text for s in candidate_spans(deps.repos.passages.list(
        "alice", result.verdicts[0].matched.observation_id))]
    assert all(counting.embedded.count(t) == 1 for t in spans)


def test_a_claim_of_several_sentences_is_matched_by_a_window_as_wide(make_deps):
    # Two-sentence windows alone would miss a figure of the third sentence ("70").
    model = judge("SUPPORTED")
    v, _, _ = check(make_deps, model, claim=PARAGRAPH.replace("opened", "expanded to 70 "
                                                              "countries and opened"),
                    page=PARAGRAPH.replace("opened", "expanded to 70 countries and opened"))
    assert v.verdict == "verified" and len(model.seen) == 1


def test_document_claims_use_the_same_spans_and_entailment(make_deps):
    model = judge("SUPPORTED")
    v, deps, _ = check(make_deps, model, page=PARAGRAPH, kind="pdf")
    assert v.verdict == "verified" and v.check == "document_passages"
    assert len(model.seen) == 1


# --- the verdict follows the judgment, and fails closed ----------------------------------------

def test_any_supported_span_verifies_and_the_match_is_its_parent_passage(make_deps):
    model = judge("CONTRADICTED", "SUPPORTED", "INSUFFICIENT_EVIDENCE")
    v, deps, result = check(make_deps, model, page="\n\n".join([SALES] * 3))
    assert v.verdict == "verified"
    refetched = deps.repos.passages.list("alice", v.matched.observation_id)
    assert v.matched.index == 1 and refetched[1].text == SALES  # the second candidate's Passage
    assert v.matched.observation_id != v.original.observation_id  # the re-fetch, not the cited one
    assert result.draft == draft_of([SALES])
    (logged,) = events(deps, "fact_check_entailment")
    assert logged["candidates"] == 3 and logged["verdicts"]["2"] == ["SUPPORTED"]


def test_the_match_of_a_sentence_is_the_whole_paragraph_passage(make_deps):
    v, deps, _ = check(make_deps, judge("SUPPORTED"), page="Intro.\n\n" + PARAGRAPH)
    matched = deps.repos.passages.list("alice", v.matched.observation_id)[v.matched.index]
    assert v.matched.index == 1 and matched.text == PARAGRAPH


@pytest.mark.parametrize("verdict", ["CONTRADICTED", "INSUFFICIENT_EVIDENCE"])
def test_no_supported_span_leaves_the_claim_unverified_and_marked(make_deps, verdict):
    v, _, result = check(make_deps, judge(verdict))
    assert v.verdict == "unverified" and verdict in v.reason
    assert v.matched is not None and v.similarity >= 0.85
    assert result.draft.sections[0].claims[0].text.endswith(" [UNVERIFIED]")
    assert result.summary["unverified"] == 1 and result.summary["pass_rate"] == 0.0


@pytest.mark.parametrize("content", [
    "not json at all",
    '{"verdicts": [{"passage": 1, "verdict": "MAYBE"}]}',
    '{"verdicts": "nope"}',
])
def test_a_malformed_reply_leaves_the_claim_unverified_and_is_logged(make_deps, content):
    v, deps, result = check(make_deps, replying(content))
    assert v.verdict == "unverified" and "entailment check failed (BadModelReply)" in v.reason
    assert result.summary["unverified"] == 1 and result.summary["unchecked"] == 0
    (failed,) = events(deps, "fact_check_entailment_failed")
    assert failed["error"].startswith("BadModelReply") and failed["claim"] == 0


def test_a_model_exception_leaves_the_claim_unverified_not_unchecked(make_deps):
    def down(messages):
        raise ConnectionError("provider unavailable")
    v, deps, result = check(make_deps, ScriptedChat(reply=down))
    assert v.verdict == "unverified" and "ConnectionError" in v.reason
    assert result.summary["pass_rate"] == 0.0
    assert "provider unavailable" in events(deps, "fact_check_entailment_failed")[0]["error"]


@pytest.mark.parametrize("content", [
    '{"verdicts": []}',
    '{"verdicts": [{"passage": 2, "verdict": "SUPPORTED"}]}',  # only one span was shown
    '{"verdicts": [{"passage": 0, "verdict": "SUPPORTED"}]}',
    '{"verdicts": [{"passage": 1}]}',  # no verdict: the schema's default is INSUFFICIENT
])
def test_omitted_or_unusable_verdicts_leave_the_claim_unverified(make_deps, content):
    v, _, _ = check(make_deps, replying(content))
    assert v.verdict == "unverified"


def test_a_span_judged_both_supported_and_contradicted_does_not_verify(make_deps):
    both = ('{"verdicts": [{"passage": 1, "verdict": "SUPPORTED"}, '
            '{"passage": 1, "verdict": "CONTRADICTED"}]}')
    v, _, _ = check(make_deps, replying(both))
    assert v.verdict == "unverified"
