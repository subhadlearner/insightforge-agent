"""The entailment judgment shared by Synthesis (T5) and the Fact-Checker (F2): one light-model
call that judges numbered passages against one statement, answered with these verdicts."""

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]

ENTAIL_SYSTEM = """You judge whether numbered passages support one evidence statement.
Reply with JSON only:
{"verdicts": [{"passage": <number>, "verdict": "SUPPORTED" | "CONTRADICTED" |
"INSUFFICIENT_EVIDENCE", "asserts_present_state": <true if the passage asserts how things
stand now, not history, a prediction or an announcement>}]}
SUPPORTED: the passage states the statement. CONTRADICTED: it states something that
conflicts with it. INSUFFICIENT_EVIDENCE: neither. Give one verdict for every passage."""


class PassageVerdict(BaseModel):
    passage: int
    verdict: Verdict = "INSUFFICIENT_EVIDENCE"
    asserts_present_state: bool = False


class EntailmentVerdicts(BaseModel):
    verdicts: list[PassageVerdict] = Field(default_factory=list)
