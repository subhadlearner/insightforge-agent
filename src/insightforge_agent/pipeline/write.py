"""WRITING: the Report draft, from the Evidence bundle alone.

The Writer never sees Passage text. Every Claim must cite an existing Evidence item and may
contain only numbers that item contains. Violations are retried with the offending Claims
listed as a constraint, up to `writer_retries` times."""

from insightforge_agent.agents.llm_json import BadModelReply, ask_json
from insightforge_agent.domain.contracts import EvidenceBundle, ReportDraft
from insightforge_agent.domain.passages import numbers_in
from insightforge_agent.pipeline.deps import Deps
from insightforge_agent.pipeline.errors import RunFailed

SYSTEM = """You write a cited intelligence report from an Evidence bundle. Reply with JSON only:
{"title": str, "summary": str, "sections": [{"heading": str, "claims":
[{"text": str, "evidence_id": str}]}]}
Rules: every claim cites exactly one evidence_id from the bundle and states only what that
item says; every number and name in a claim must appear in the cited item; use one section
per bundle section, with its heading. Do not invent evidence ids."""


def violations(draft: ReportDraft, bundle: EvidenceBundle) -> list[str]:
    items = {i.id: i for s in bundle.sections for i in s.items}
    problems = []
    claims = [c for s in draft.sections for c in s.claims]
    if not claims:
        problems.append("the report has no claims")
    for c in claims:
        item = items.get(c.evidence_id)
        if item is None:
            problems.append(f"claim {c.text!r} cites unknown evidence_id {c.evidence_id!r}")
            continue
        extra = numbers_in(c.text) - numbers_in(item.statement)
        if extra:
            problems.append(
                f"claim {c.text!r} has values {sorted(extra)} not in evidence {item.id}")
    return problems


def render_bundle(bundle: EvidenceBundle) -> str:
    lines = []
    for s in bundle.sections:
        lines.append(f"## {s.heading} (confidence {s.confidence})")
        lines += [f"- [{i.id}] ({i.confidence}) {i.statement}" for i in s.items]
    return "\n".join(lines)


def write_report(
    deps: Deps, owner_id: str, run_id: str, brief: str, bundle: EvidenceBundle, gaps: list[str],
) -> ReportDraft:
    constraint = ""
    last: list[str] = []
    for attempt in range(deps.writer_retries + 1):
        user = f"Brief: {brief}\n\nEvidence bundle:\n{render_bundle(bundle)}{constraint}"
        try:
            draft = ask_json(deps.writer_model, SYSTEM, user, ReportDraft)
            last = violations(draft, bundle)
        except BadModelReply as e:
            last = [f"the reply was not valid report JSON: {e}"]
        if not last:
            return draft.model_copy(update={"gaps": gaps})
        deps.log(owner_id, run_id, "writer_violations", attempt=attempt + 1, problems=last)
        constraint = "\n\nYour previous draft broke these rules. Fix them:\n" + "\n".join(
            f"- {p}" for p in last)
    raise RunFailed(f"the Writer broke the citation rules after {deps.writer_retries} retries")
