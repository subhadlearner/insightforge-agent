"""Regression tests for the first capped live end-to-end Run after PR #77's reviews
(run_c11947913dc5, 2026-10-21: COMPLETE, $0.317). Its Report contradicted itself: every
requirement was `verification_incomplete` ("this research did not establish it") above Claims
answering each, and 25 Claims were replaced verbatim, leaving duplicates.

A seven-call replay of the failed checks, inputs rebuilt from the saved Report body, captured the
model's replies (tests/fixtures/research_integrity/run_c11947913dc5_checks.json; text only).
They show three structural rules the model does not follow, none of them a semantic error:
- coverage: a quote joining non-adjacent words of the Brief with "...", needed when a Brief
  names two companies once and three measures after them;
- answers: parts worded as noun phrases ("the pricing strategy Tesla uses ..."), not copied;
- answers: entries only for the statements that answer, in all six replies.
The Writer's replacements came from the number and name rules ("One item reports", "EV",
a publication date), applied as a veto with a verbatim fallback that never checked whether the
item was already stated.

Block 1 replays the saved replies through the production functions; the judgments after them
(the coverage of each split, the verifier) were never made in that Run and are scripted here,
so these tests prove the replies are now read, not what the later judgments would say."""

import json
import re
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage

from insightforge_agent.domain.contracts import EvidenceItem, PassageRef
from insightforge_agent.domain.requirements import Requirement, RequirementLedger
from insightforge_agent.domain.support import find_excerpt
from insightforge_agent.pipeline import requirements as rq
from insightforge_agent.pipeline.write import write_report
from tests.pipeline.test_extraction_synthesis import world  # noqa: F401
from tests.pipeline.test_research_integrity_review import judge
from tests.pipeline.test_t6_writer_fact_check import evidence
from tests.scripted import (
    ANSWERS_PREFIX,
    COVERAGE_PREFIX,
    VERIFY_PREFIX,
    ScriptedChat,
    coverage_verdict,
    verify_verdicts,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "research_integrity" / "run_c11947913dc5_checks.json"
CALLS = json.loads(FIXTURE.read_text(encoding="utf-8"))
BRIEF = ("Compare BYD and Tesla in battery electric vehicles: 2025 sales volume, pricing "
         "strategy and battery technology.")
REF = PassageRef(observation_id="o", index=0)


def replying(*replies) -> ScriptedChat:
    return ScriptedChat(script=[AIMessage(content=json.dumps(r)) for r in replies])


def question_and_items(request: str) -> tuple[str, list[EvidenceItem]]:
    question = request.split("\n", 1)[0].removeprefix("Question: ")
    statements = re.findall(r"^\[\d+\] (.+)$", request, re.MULTILINE)
    return question, [EvidenceItem(id=f"ev_{n}", subtask_id="s1", aspect_id="a1",
                                   statement=s, confidence="MEDIUM", supporting=[REF],
                                   primary=REF, requirement_ids=["R1"])
                      for n, s in enumerate(statements)]


ANSWER_CALLS = CALLS[1:]


# ==== 1. The saved replies, read by the production functions ================================

def test_the_saved_brief_coverage_reply_is_read_and_finds_every_ask_covered(make_deps):
    request, reply = CALLS[0]["request"], CALLS[0]["reply"]
    questions = re.findall(r"^\[\d+\] (.+)$", request.split("\n\nItems:\n", 1)[1], re.MULTILINE)
    elided = [a["quote"] for a in reply["asks"] if "..." in a["quote"]]
    assert elided and all(find_excerpt(q, BRIEF) is None for q in elided)  # coverage-1's refusal
    model = replying(reply)
    out = rq.check_coverage(make_deps(light_model=model), "alice", "run_1", "Text", BRIEF,
                            questions, counted=False, of="brief")
    assert str(model.seen[0][-1].content) == request  # the replay rebuilt the Run's request
    assert out.failed == "" and out.uncovered == () and out.covering == frozenset(range(1, 7))


def test_each_saved_answer_reply_is_read_and_a_statement_left_out_states_no_part(make_deps):
    for call in ANSWER_CALLS:
        question, items = question_and_items(call["request"])
        req = Requirement(id="R1", question=question, aspect_ids=["a1"])
        model = replying(call["reply"])
        got = rq._propose(make_deps(light_model=model), "alice", "run_1", req, items)
        assert str(model.seen[0][-1].content) == call["request"], question
        assert not isinstance(got, str), (question, got)
        parts, states = got
        assert parts == call["reply"]["parts"], question
        listed = {a["statement"] for a in call["reply"]["answers"]}
        assert all(states[f"ev_{n - 1}"] == [] for n in range(1, len(items) + 1)
                   if n not in listed), question


def test_with_the_later_judgments_scripted_every_requirement_is_now_decided(make_deps):
    """The saved proposals, then a coverage check that finds the one part covers the question
    and a verifier that accepts every claimed answer (both scripted: not the Run's)."""
    by_question = {question_and_items(c["request"])[0]: c["reply"] for c in ANSWER_CALLS}
    reqs, linked = [], []
    for k, call in enumerate(ANSWER_CALLS, start=1):
        question, items = question_and_items(call["request"])
        reqs.append(Requirement(id=f"R{k}", question=question, aspect_ids=["a1"]))
        linked += [i.model_copy(update={"id": f"r{k}_{i.id}", "requirement_ids": [f"R{k}"]})
                   for i in items]
    ledger = RequirementLedger(origin="brief", requirements=reqs)

    def chat(messages):
        system = str(next(m.content for m in messages if isinstance(m, SystemMessage)))
        user = str(messages[-1].content)
        if system.startswith(ANSWERS_PREFIX):
            return AIMessage(content=json.dumps(
                by_question[user.split("\n", 1)[0].removeprefix("Question: ")]))
        if system.startswith(COVERAGE_PREFIX):
            return AIMessage(content=json.dumps(coverage_verdict(user)))
        assert system.startswith(VERIFY_PREFIX)
        return AIMessage(content=json.dumps(verify_verdicts(user)))
    out = rq.confirm_answers(make_deps(light_model=ScriptedChat(reply=chat, checks=False)),
                             "alice", "run_1", ledger, linked)
    assert out.failed == []
    assert all(out.answers[r.id] for r in reqs)  # each had at least one claimed answer


# ==== 2. Quotes with elisions: still the text's own words, in order ==========================

def test_an_elided_quote_must_be_the_texts_words_in_order():
    assert rq.quoted("BYD and Tesla in battery electric vehicles: ... pricing strategy", BRIEF)
    assert rq.quoted("BYD and Tesla … battery technology", BRIEF)
    assert not rq.quoted("pricing strategy ... BYD and Tesla", BRIEF)  # out of order
    assert not rq.quoted("BYD and Tesla ... Model Y pricing", BRIEF)  # a piece not in the text
    assert not rq.quoted("... pricing strategy", BRIEF)  # an empty piece


def test_an_elided_quote_not_in_the_text_still_fails_the_coverage_check(make_deps):
    reply = {"asks": [{"quote": "BYD and Tesla ... Model Y pricing", "covered_by": [1]}]}
    out = rq.check_coverage(make_deps(light_model=replying(reply)), "alice", "run_1", "Text",
                            BRIEF, ["What pricing strategy does BYD use?"], counted=False)
    assert out.failed == "an ask is not words of the text"


# ==== 3. Later batches keep the first batch's parts ==========================================

def test_a_later_batch_is_given_the_first_batchs_parts_and_cannot_resplit(make_deps):
    question = "What was Tesla's 2025 battery electric vehicle sales volume?"
    first = {"parts": ["Tesla's 2025 BEV sales volume"],
             "answers": [{"statement": 1, "parts": [1], "excerpt": "Tesla sold 1.64 million"}]}
    later = {"parts": ["the sales volume of Tesla in 2025"], "answers": []}  # reworded: ignored
    model = replying(first, later)
    long = "Tesla's Model 3 and Model Y were built in Shanghai and Berlin. " * 60
    items = [EvidenceItem(id=f"ev_{n}", subtask_id="s1", aspect_id="a1", statement=s,
                          confidence="MEDIUM", supporting=[REF], primary=REF)
             for n, s in enumerate(["Tesla sold 1.64 million BEVs in 2025.", long])]
    deps = make_deps(light_model=model, passage_token_cap=600)
    got = rq._propose(deps, "alice", "run_1", Requirement(id="R1", question=question,
                                                          aspect_ids=["a1"]), items)
    assert len(model.seen) == 2
    assert "1. Tesla's 2025 BEV sales volume" in str(model.seen[1][-1].content)
    assert got == (["Tesla's 2025 BEV sales volume"], {"ev_0": [1], "ev_1": []})


# ==== 4. The Writer: the number and name rules advise; the semantic check decides =============

STATEMENT = "BYD sold 4.27 million vehicles in 2025."
PRICE = "BYD cut the Seagull price to 9,700 dollars."


def writer_of(claims, summary=STATEMENT) -> ScriptedChat:
    return ScriptedChat(reply=lambda m: AIMessage(content=json.dumps(
        {"title": "T", "summary": summary,
         "sections": [{"heading": "Sales", "claims": claims}]})))


def texts(draft):
    return [(c.evidence_id, c.text) for s in draft.sections for c in s.claims]


def events(deps, type_):
    return [e.payload for e in deps.repos.events.read_after("alice", "run_1") if e.type == type_]


def test_a_supported_claim_the_word_rules_refuse_is_published_in_the_writers_words(make_deps):
    """As in run_c11947913dc5: a sentence opening "One item reports" and saying "EVs" breaks the
    name rule, and once was replaced verbatim. Its cited Passage supports it: it stays."""
    worded = "One item reports that BYD sold 4.27 million EVs in 2025."
    deps = make_deps(light_model=judge(verdict=lambda s, t: "SUPPORTED"),
                     writer_model=writer_of([{"text": worded, "evidence_id": "ev_0"},
                                             {"text": PRICE, "evidence_id": "ev_1"}]))
    _, bundle = evidence(deps, [STATEMENT, PRICE])
    draft = write_report(deps, "alice", "run_1", "BYD", bundle, [])
    assert texts(draft) == [("ev_0", worded), ("ev_1", PRICE)]
    (flagged,) = events(deps, "writer_claims_ungrounded")
    assert [c["evidence_id"] for c in flagged["claims"]] == ["ev_0"]
    assert "claims_left_to_semantic_check:1" in draft.quality
    assert not any(q.startswith("claims_restated_verbatim") for q in draft.quality)


def test_two_unsupported_claims_on_one_item_are_both_omitted_not_restated(make_deps):
    """The run's duplicates: two Claims citing one item, both rewritten to its statement. Since
    run_846d12a50177 a rejected Claim is omitted, never restated, so neither duplicates."""
    deps = make_deps(light_model=judge(),  # supports only a statement the Passage contains
                     writer_model=writer_of([
                         {"text": "BYD's 4.27 million beat Tesla's 1.64 million.",
                          "evidence_id": "ev_0"},
                         {"text": "BYD outsold Tesla with 4.27 million vehicles.",
                          "evidence_id": "ev_0"},
                         {"text": PRICE, "evidence_id": "ev_1"}]))
    _, bundle = evidence(deps, [STATEMENT, PRICE])
    draft = write_report(deps, "alice", "run_1", "BYD", bundle, [])
    assert texts(draft) == [("ev_1", PRICE)]
    (checked,) = events(deps, "writer_claims_checked")
    assert [r["action"] for r in checked["rejected"]] == ["omitted", "omitted"]


def test_an_unsupported_claim_on_an_item_a_published_claim_states_is_removed(make_deps):
    """A faithful rewording stays; a second Claim on the same item that fails is not restated
    beside it (the run's Blade Battery pair: the same item, twice, in different words)."""
    reworded = "In 2025, BYD sold 4.27 million vehicles."
    comparison = "BYD's 4.27 million vehicles were far ahead of Tesla in 2025."
    deps = make_deps(light_model=judge(verdict=lambda s, t: "SUPPORTED" if s == reworded
                                       else "INSUFFICIENT_EVIDENCE"),
                     writer_model=writer_of([{"text": reworded, "evidence_id": "ev_0"},
                                             {"text": comparison, "evidence_id": "ev_0"},
                                             {"text": PRICE, "evidence_id": "ev_1"}]))
    _, bundle = evidence(deps, [STATEMENT, PRICE])
    draft = write_report(deps, "alice", "run_1", "BYD", bundle, [])
    assert texts(draft) == [("ev_0", reworded), ("ev_1", PRICE)]
    (checked,) = events(deps, "writer_claims_checked")
    assert [(r["original_text"], r["action"]) for r in checked["rejected"]] == [
        (comparison, "omitted")]
