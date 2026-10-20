"""The shared entailment instructions (Synthesis and the Fact-Checker) after T18 Stage B-1
(B1-01, B1-02): SUPPORTED needs the whole statement, about the same names, no stronger than the
passage. Offline, these only pin what the model is told; whether it follows it is measured by an
authorised B-1 run, never claimed here."""

import json

from langchain_core.messages import AIMessage

from insightforge_agent.pipeline import fact_check, synthesize
from insightforge_agent.pipeline.entailment import ENTAIL_SYSTEM
from tests.scripted import ScriptedChat


def test_supported_needs_the_same_named_entities():  # B1-01: a publication swapped
    for name in ("company", "organisation", "person", "product", "publication", "place"):
        assert name in ENTAIL_SYSTEM
    assert "different, even related, name is not\nsupport" in ENTAIL_SYSTEM


def test_supported_is_never_stronger_than_the_passage():  # B1-02: scope or effect overstated
    for word in ('"all"', '"every"', '"only"', '"always"', '"never"', '"entirely"'):
        assert word in ENTAIL_SYSTEM
    assert "not a duty,\nright or exemption the passage does not state" in ENTAIL_SYSTEM
    assert "supports only part of the statement or a\nweaker version of it" in ENTAIL_SYSTEM


def test_paraphrase_and_the_historical_framing_still_count():  # the positives keep passing
    assert "wording may differ, meaning\nmay not" in ENTAIL_SYSTEM
    assert "introduced as what an earlier\nreport found asserts that finding" in ENTAIL_SYSTEM


def test_the_reply_contract_is_unchanged():
    for field in ('"verdicts"', '"passage"', '"verdict"', '"asserts_present_state"',
                  "SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE",
                  "Give one verdict for every passage"):
        assert field in ENTAIL_SYSTEM


def test_both_consumers_send_the_one_shared_instruction(make_deps):
    """Synthesis and the Fact-Checker import the same object, so one change reaches both."""
    assert synthesize.ENTAIL_SYSTEM is ENTAIL_SYSTEM is fact_check.ENTAIL_SYSTEM
    from insightforge_agent.domain.models import Passage
    from insightforge_agent.domain.passages import candidate_spans

    seen = []

    def reply(messages):
        seen.append(messages[0].content)
        return AIMessage(content=json.dumps({"verdicts": [{"passage": 1,
                                                           "verdict": "SUPPORTED"}]}))

    deps = make_deps(light_model=ScriptedChat(reply=reply))
    span = candidate_spans([Passage(observation_id="o", index=0, text="BYD sold 4.27 million.")],
                           1)[0]
    fact_check._entailed(deps, "alice", "run", "BYD sold 4.27 million.", [(1.0, span)])
    assert seen == [ENTAIL_SYSTEM]
