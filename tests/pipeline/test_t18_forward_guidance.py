"""T18 Stage C: forward guidance, now in the extraction prompt (ADR-0006: Extraction is the only
pass proposing facts).

The rerun's document Run retrieved the report's outlook Passage for four Sub-tasks but never
drafted it. The prompt that proposes facts asks for stated management outlook or forward
guidance as its own fact. These scripted tests show the instruction is sent and that a guidance
fact goes through the usual provenance, figure and support checks, and that invented guidance
does not. They cannot show that the live model follows the instruction: that is a
live-evaluation question."""

import pytest
from langchain_core.messages import SystemMessage

from insightforge_agent.pipeline.extract import SYSTEM as EXTRACT_SYSTEM
from tests.pipeline.test_extraction_synthesis import world  # noqa: F401
from tests.pipeline.test_t18_document_entailment import CHUNKS, run, strict

GUIDANCE = "Northwind Health expects fiscal 2026 revenue growth of 9 to 11 percent."
INSTRUCTION = ("When a passage states management outlook or forward guidance, extract it as its "
               "own fact with its stated period, worded as expected; do not infer, calculate or "
               "invent guidance.")


def guidance_drafts(statement, chunk):
    """Proposes one guidance fact from the shown Passage holding `chunk`, with its period."""
    def handler(ps, user):
        return [{"statement": statement, "passages": [n for n, t in ps if t == chunk],
                 "entities": ["Northwind Health"], "entity": "Northwind Health",
                 "predicate": "revenue_growth_guidance", "value": "9 to 11 percent",
                 "as_of_period": "2026"}]
    return handler


def test_the_extraction_prompt_asks_for_stated_guidance_generically():
    assert INSTRUCTION in " ".join(EXTRACT_SYSTEM.split())
    assert "Northwind" not in EXTRACT_SYSTEM and "9 to 11" not in EXTRACT_SYSTEM


def test_the_instruction_is_sent_with_every_extraction_call(world):
    _, model = run(world, CHUNKS + [GUIDANCE], guidance_drafts(GUIDANCE, GUIDANCE))
    systems = [str(next(m.content for m in c if isinstance(m, SystemMessage)))
               for c in model.seen]
    extraction = [s for s in systems if s.startswith("You extract facts")]
    assert extraction and all(INSTRUCTION in " ".join(s.split()) for s in extraction)


def test_stated_guidance_passes_the_usual_checks_and_keeps_its_period(world):
    kept, _ = run(world, CHUNKS + [GUIDANCE], guidance_drafts(GUIDANCE, GUIDANCE))
    assert kept == [GUIDANCE]
    (diag,) = world.events("evidence_support")
    assert diag["outcome"] == "supported"
    assert world.events("evidence_dropped") == []


@pytest.mark.parametrize("cited", [CHUNKS[0], CHUNKS[3]], ids=["revenue", "employees"])
def test_guidance_a_document_does_not_state_is_never_retained(world, cited):
    # An extractor that invents the outlook from a Passage without one: the figure check drops
    # it before any judge, since the figures are not in the cited Passage.
    kept, model = run(world, CHUNKS, guidance_drafts(GUIDANCE, cited), support=strict)
    assert kept == []
    (dropped,) = world.events("evidence_dropped")
    assert "value not in Passage" in str(dropped["rejected"])
    assert model.calls("You check whether") == []
