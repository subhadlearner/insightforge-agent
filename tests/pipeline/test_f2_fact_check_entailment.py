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
from insightforge_agent.embeddings import HashingEmbedder
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


def check(make_deps, model, claim=SALES, page=SALES, kind="web"):
    deps = make_deps(fetcher=Fetcher({URL: page}), light_model=model)
    obs, bundle = evidence(deps, [page], kind=kind)
    result = fact_check(deps, "alice", "run_1", draft_of([claim]), bundle)
    (v,) = result.verdicts
    return v, deps, result


def events(deps, type_):
    return [e.payload for e in deps.repos.events.read_after("alice", "run_1") if e.type == type_]


# --- no model call when the deterministic gates leave no span ---------------------------------

def test_no_entailment_call_when_no_span_clears_the_threshold(make_deps):
    model = judge("SUPPORTED")
    v, deps, _ = check(make_deps, model, claim="Tesla opened a factory in Berlin.")
    assert v.verdict == "unverified" and v.similarity < 0.85
    assert v.reason == "no Passage is similar enough to the Claim"
    assert model.seen == [] and events(deps, "fact_check_entailment") == []


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
    v, _, _ = check(make_deps, model, page=PARAGRAPH)
    assert v.verdict == "verified" and v.similarity == pytest.approx(1.0)
    texts = [t for _, t in numbered(model.seen[0])]
    assert texts[0] == SALES and PARAGRAPH not in texts


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
    assert [span.text for _, span in s.candidates] == ["Near miss.", "Supporting sentence."]
    assert s.candidates[1][1].passage == good


def test_the_payload_is_trimmed_to_the_per_call_cap_keeping_the_best_span(make_deps):
    model = judge("SUPPORTED")
    deps = make_deps(fetcher=Fetcher({URL: "\n\n".join([SALES] * 5)}), light_model=model,
                     passage_token_cap=1)
    _, bundle = evidence(deps, ["\n\n".join([SALES] * 5)])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle).verdicts
    assert v.verdict == "verified" and len(numbered(model.seen[0])) == 1


def test_a_broken_invariant_in_the_model_slot_is_not_swallowed(make_deps):
    def tripwire(messages):
        raise AssertionError("must not use a model")
    with pytest.raises(AssertionError, match="must not use a model"):
        check(make_deps, ScriptedChat(reply=tripwire))


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
