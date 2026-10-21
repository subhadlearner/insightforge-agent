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
SUPPORTED: the passage states everything the statement asserts; wording may differ, meaning
may not. Every named company, organisation, person, product, publication or place must be the
one the passage says it about: the same fact told of a different, even related, name is not
support. Nothing may be stronger than the passage: not wider in scope ("all", "every",
"only", "always", "never", "entirely"), not a larger or more absolute effect, and not a duty,
right or exemption the passage does not state. A statement introduced as what an earlier
report found asserts that finding: judge the finding.
CONTRADICTED: it states something that conflicts with the statement.
INSUFFICIENT_EVIDENCE: neither, including when it supports only part of the statement or a
weaker version of it. Give one verdict for every passage."""


class PassageVerdict(BaseModel):
    passage: int
    verdict: Verdict = "INSUFFICIENT_EVIDENCE"
    asserts_present_state: bool = False


class EntailmentVerdicts(BaseModel):
    verdicts: list[PassageVerdict] = Field(default_factory=list)
