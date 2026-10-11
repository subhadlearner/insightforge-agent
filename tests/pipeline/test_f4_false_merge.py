"""F4: the five reviewed DIFFERENT_FACT PAIR cases that the merge key folds together, driven
through the real `synthesize()` rather than `merge_identity` alone.

Each case is two web Sources on different domains (credibility 0.9, so two supporting Sources
would be HIGH). The light model is scripted: the draft reply carries the case's reviewed
statements and structured fields, and the entailment reply is the semantically correct verdict
for each (statement, Passage) pair. What is under test is the production Merge and validation
mechanics, not a model's judgment. Spies on `_draft` and `_validate` record what Merge produced."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from insightforge_agent.domain.contracts import (
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    PassageRef,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.pipeline import synthesize as synth
from tests.pipeline.test_extraction_synthesis import light

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
OWNER, RUN = "alice", "run1"


@dataclass(frozen=True)
class Side:
    url: str
    passage: str
    statement: str
    period: str  # the as_of_period the drafting model returns
    published: datetime | None = None
    present: bool = False  # the entailment judge's asserts_present_state


@dataclass(frozen=True)
class Case:
    id: str
    entity: str
    predicate: str
    value: str
    a: Side
    b: Side
    scope: str = ""

    def supports(self, statement: str, passage: str) -> bool:
        """The semantically correct judgment: a statement is SUPPORTED by the Passage it was
        drafted from; the other side's Passage supports it only when it says the same thing."""
        return any(s.statement == statement and s.passage == passage for s in (self.a, self.b))


def _d(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


P_TA_02 = Case(
    "p-ta-02", "Orrin Bio", "employees", "310",
    Side("https://a.example/x", "Published 3 May 2021. Orrin Bio has 310 employees and plans to expand.",
         "Orrin Bio has 310 employees.", "", _d(2021, 5, 3), present=True),
    Side("https://b.test/y", "Published 9 June 2025. Orrin Bio has 310 employees after a round of layoffs.",
         "Orrin Bio has 310 employees.", "", _d(2025, 6, 9), present=True))
P_TA_03 = Case(
    "p-ta-03", "Kestrel Air", "projected passengers", "35 million",
    Side("https://a.example/x", "Published 12 February 2023. Kestrel Air will carry 35 million passengers this year.",
         "Kestrel Air will carry 35 million passengers this year.", "this year", _d(2023, 2, 12)),
    Side("https://b.test/y", "Published 20 February 2024. Kestrel Air will carry 35 million passengers this year.",
         "Kestrel Air will carry 35 million passengers this year.", "this year", _d(2024, 2, 20)))
# The PAIR cases below carry no Passage; each side's Passage is its reviewed statement, and the
# drafting model returns the reviewed non-canonical period wording.
P_PS_01 = Case(
    "p-ps-01", "Helix Foods", "revenue", "$2.1 billion",
    Side("https://a.example/x", "Helix Foods' revenue was $2.1 billion in fiscal 2023.",
         "Helix Foods' revenue was $2.1 billion in fiscal 2023.", "fiscal 2023"),
    Side("https://b.test/y", "Helix Foods' revenue was $2.1 billion in fiscal 2024.",
         "Helix Foods' revenue was $2.1 billion in fiscal 2024.", "fiscal 2024"))
P_PS_04 = Case(
    "p-ps-04", "Orrin Bio", "employees", "310",
    Side("https://a.example/x", "Orrin Bio had 310 employees at the end of 2022.",
         "Orrin Bio had 310 employees at the end of 2022.", "end of 2022"),
    Side("https://b.test/y", "Orrin Bio had 310 employees at the end of 2024.",
         "Orrin Bio had 310 employees at the end of 2024.", "end of 2024"))
P_FM_02 = Case(
    "p-fm-02", "Aldine Capital", "closed fund", "$400 million",
    Side("https://a.example/x", "Aldine Capital, the London asset manager, closed a $400 million fund in 2024.",
         "Aldine Capital, the London asset manager, closed a $400 million fund in 2024.", "2024"),
    Side("https://b.test/y", "Aldine Capital, the Houston energy lender, closed a $400 million fund in 2024.",
         "Aldine Capital, the Houston energy lender, closed a $400 million fund in 2024.", "2024"))


@dataclass
class Outcome:
    drafted: int
    merged: list[list[str]]  # per candidate reaching validation: the sides it cites
    items: list[EvidenceItem]
    side_of: dict[str, str] = field(default_factory=dict)  # observation id -> "a" / "b"

    def sides(self, item: EvidenceItem) -> list[str]:
        return sorted(self.side_of[r.observation_id] for r in item.supporting)

    def statements(self) -> set[str]:
        return {i.statement for i in self.items}


def run(case: Case, make_deps, monkeypatch) -> Outcome:
    deps = make_deps(now=lambda: NOW)
    side_of, refs = {}, []
    for tag, side in (("a", case.a), ("b", case.b)):
        obs = deps.store.ingest_web(
            owner_id=OWNER, run_id=RUN, url=side.url, title=side.url, body=side.passage,
            fetched_at=NOW, credibility_score=0.9, published_at=side.published)
        side_of[obs.id] = tag
        refs.append((obs, side))
    task = SubTask(id="s1", aspect_id="a1", query="q", source_type="web",
                   time_horizon_months=12, priority=1)
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="A")], sub_tasks=[task])
    research = ResearchResults(results=[SubTaskResult(
        subtask_id="s1", status="succeeded",
        source_refs=[SourceRef(source_id=o.source_id, title=s.url, summary="s") for o, s in refs])])
    # The Extraction facts mirror the drafted fields, as the extraction model would give them.
    extraction = ExtractionResult(facts=[ExtractedFact(
        id=f"f{tag}", source_id=o.source_id, passage=PassageRef(observation_id=o.id, index=0),
        statement=s.statement, entity=case.entity, predicate=case.predicate, scope=case.scope,
        value=case.value, period=s.period or "UNKNOWN") for tag, (o, s) in zip("ab", refs)])

    by_passage = {case.a.passage: case.a, case.b.passage: case.b}

    def draft(numbered, user):
        return [{"statement": by_passage[t].statement, "passages": [n],
                 "entities": [case.entity], "entity": case.entity, "predicate": case.predicate,
                 "value": case.value, "scope": case.scope,
                 "as_of_period": by_passage[t].period or "UNKNOWN"} for n, t in numbered]

    def entail(numbered, user):
        statement = re.match(r"Statement: (.+)", user).group(1)
        return [{"passage": n,
                 "verdict": "SUPPORTED" if case.supports(statement, t) else "INSUFFICIENT_EVIDENCE",
                 "asserts_present_state": by_passage[t].present} for n, t in numbered]

    deps.light_model = light(draft=draft, entail=entail)

    drafted, merged = [], []
    real_draft, real_validate = synth._draft, synth._validate

    def spy_draft(*a, **k):
        out = real_draft(*a, **k)
        drafted.extend(out)
        return out

    def spy_validate(deps_, owner, run_id, c, *rest):
        merged.append(sorted(side_of[r.observation_id] for r in c.refs))
        return real_validate(deps_, owner, run_id, c, *rest)

    monkeypatch.setattr(synth, "_draft", spy_draft)
    monkeypatch.setattr(synth, "_validate", spy_validate)
    bundle = synth.synthesize(deps, OWNER, RUN, "brief", plan, research, extraction)
    items = [i for s in bundle.sections for i in s.items]
    return Outcome(drafted=len(drafted), merged=merged, items=items, side_of=side_of)


def summary(o: Outcome) -> tuple:
    """(drafted, the sides each merged candidate cites, [(supporting sides, Confidence)])."""
    return o.drafted, o.merged, sorted((o.sides(i), i.confidence) for i in o.items)


# ---- Phase 1: production behaviour before the F4 change ------------------------------------

@pytest.mark.parametrize("case", [P_TA_02, P_TA_03], ids=lambda c: c.id)
def test_phase1_temporal_observations_years_apart_become_one_high_item(case, make_deps, monkeypatch):
    o = run(case, make_deps, monkeypatch)
    assert summary(o) == (2, [["a", "b"]], [(["a", "b"], "HIGH")])
    (item,) = o.items
    assert item.as_of_period == "UNKNOWN" and item.temporally_ambiguous


@pytest.mark.parametrize("case", [P_PS_01, P_PS_04], ids=lambda c: c.id)
def test_phase1_noncanonical_periods_merge_and_the_second_fact_is_lost(case, make_deps, monkeypatch):
    o = run(case, make_deps, monkeypatch)
    # The literal check rejects side b for the kept statement (its year is not in Passage b),
    # so there is no false corroboration, but side b's own fact is gone and nothing logs it.
    assert summary(o) == (2, [["a", "b"]], [(["a"], "MEDIUM")])
    assert o.statements() == {case.a.statement}


def test_phase1_same_name_entities_merge_and_the_second_fact_is_lost(make_deps, monkeypatch):
    o = run(P_FM_02, make_deps, monkeypatch)
    assert summary(o) == (2, [["a", "b"]], [(["a"], "MEDIUM")])
    assert o.statements() == {P_FM_02.a.statement}  # "London" is not in Passage b


# The same Passages, but the drafting model leaves the descriptor out of both statements.
# Nothing lexical then tells the companies apart, and each Passage does state the statement.
P_FM_02_BARE = Case(
    "p-fm-02-bare", "Aldine Capital", "closed fund", "$400 million",
    Side(P_FM_02.a.url, P_FM_02.a.passage, "Aldine Capital closed a $400 million fund in 2024.", "2024"),
    Side(P_FM_02.b.url, P_FM_02.b.passage, "Aldine Capital closed a $400 million fund in 2024.", "2024"))


def test_phase1_same_name_entities_without_descriptors_are_falsely_corroborated(
        make_deps, monkeypatch):
    o = run(P_FM_02_BARE, make_deps, monkeypatch)
    assert summary(o) == (2, [["a", "b"]], [(["a", "b"], "HIGH")])
