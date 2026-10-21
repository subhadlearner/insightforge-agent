"""F4: the five reviewed DIFFERENT_FACT PAIR cases that the merge key folds together, driven
through the real `synthesize()` rather than `merge_identity` alone.

Each case is two web Sources on different domains (credibility 0.9, so two supporting Sources
would be HIGH). The light model is scripted: the reviewed statements and structured fields are
what Extraction proposes (since ADR-0006 the only pass proposing facts, one Passage each), and
the support reply is the semantically correct verdict for each (statement, Passage) pair. What
is under test is the production Merge and validation mechanics, not a model's judgment. Spies
on `_candidates` and `_validate` record what Merge produced.

Rule under test (F4): a structured candidate whose stated period is UNKNOWN merges only with
candidates citing exactly the same Observations. Explicit periods and statement-keyed
candidates merge as before."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from insightforge_agent.domain.contracts import (
    EvidenceItem,
    PassageRef,
    ResearchResults,
    SourceRef,
    SubTaskResult,
)
from insightforge_agent.domain.plan import Aspect, SubTask, SubTaskPlan
from insightforge_agent.pipeline import synthesize as synth
from insightforge_agent.pipeline.extract import extract
from tests.pipeline.test_extraction_synthesis import light

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
OWNER, RUN = "alice", "run1"


@dataclass(frozen=True)
class Side:
    url: str
    passage: str
    statement: str
    period: str  # the period the extraction model returns
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
# extraction model returns the reviewed non-canonical period wording.
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
# The same Passages, but the extraction model leaves the descriptor out of both statements.
# Nothing lexical then tells the companies apart, and each Passage does state the statement.
P_FM_02_BARE = Case(
    "p-fm-02-bare", "Aldine Capital", "closed fund", "$400 million",
    Side(P_FM_02.a.url, P_FM_02.a.passage, "Aldine Capital closed a $400 million fund in 2024.", "2024"),
    Side(P_FM_02.b.url, P_FM_02.b.passage, "Aldine Capital closed a $400 million fund in 2024.", "2024"))


# The real stage functions, captured once so repeated runs in one test never nest the spies.
_REAL_CANDIDATES, _REAL_VALIDATE = synth._candidates, synth._validate


@dataclass(frozen=True)
class Drafted:
    id: str
    refs: tuple[PassageRef, ...]


@dataclass
class Outcome:
    drafted: list[Drafted]  # the candidates `_candidates` returned, in order, before Merge
    merged: list[list[str]]  # per candidate reaching validation: the Sources (tags) it cites
    items: list[EvidenceItem]
    tag_of: dict[str, str] = field(default_factory=dict)  # observation id -> page tag
    events: list[tuple[str, dict]] = field(default_factory=list)  # (type, payload) logged by the Run

    def logged(self, type_: str) -> list[dict]:
        return [p for t, p in self.events if t == type_]

    def tags(self, item: EvidenceItem) -> list[str]:
        return sorted(self.tag_of[r.observation_id] for r in item.supporting)

    def statements(self) -> set[str]:
        return {i.statement for i in self.items}

    def summary(self) -> tuple:
        """(drafted, the tags each merged candidate cites, [(supporting tags, Confidence)])."""
        return (len(self.drafted), sorted(self.merged),
                sorted((self.tags(i), i.confidence) for i in self.items))


def synth_run(make_deps, monkeypatch, pages: dict[str, tuple[str, str, datetime | None]],
              draft: Callable, entail: Callable, *, found: dict[str, list[str]] | None = None
              ) -> Outcome:
    """Run the real `extract` and `synthesize` over web pages `{tag: (url, body, published)}`.
    `found` maps Sub-task id -> page tags (default: one Sub-task finding every page).

    `draft(numbered, user)` scripts the reviewed proposals as the pre-ADR-0006 drafting call
    saw them: every Passage of every page, numbered 1..n in page order. Each proposal becomes
    one extracted fact per Passage it cites, returned by that Passage's own Source's extraction
    call (Sources are extracted in page order, one call each)."""
    deps = make_deps(now=lambda: NOW)
    obs_of = {tag: deps.store.ingest_web(
        owner_id=OWNER, run_id=RUN, url=url, title=url, body=body, fetched_at=NOW,
        credibility_score=0.9, published_at=published)
        for tag, (url, body, published) in pages.items()}
    found = found or {"s1": list(pages)}
    tasks = [SubTask(id=sid, aspect_id="a1", query=f"q {sid}", source_type="web",
                     time_horizon_months=12, priority=n + 1) for n, sid in enumerate(found)]
    plan = SubTaskPlan(aspects=[Aspect(id="a1", name="A")], sub_tasks=tasks)
    research = ResearchResults(results=[SubTaskResult(
        subtask_id=sid, status="succeeded",
        source_refs=[SourceRef(source_id=obs_of[t].source_id, title=t, summary="s") for t in tags])
        for sid, tags in found.items()])
    shown = [(tag, p.index, p.text) for tag in pages
             for p in deps.repos.passages.list(OWNER, obs_of[tag].id)]
    proposals = draft([(n, t) for n, (_, _, t) in enumerate(shown, start=1)], "")
    order = list(dict.fromkeys(t for tags in found.values() for t in tags))
    calls = []

    def extract_(numbered, user):
        tag = order[len(calls)]
        calls.append(tag)
        out = []
        for d in proposals:
            for n in d.get("passages", []):
                page, index, _ = shown[n - 1]
                if page == tag:
                    out.append({"passage_index": index, "statement": d["statement"],
                                **{k: d[k] for k in ("entities", "entity", "predicate", "value",
                                                     "scope") if k in d},
                                "period": d.get("as_of_period", "UNKNOWN")})
        return out

    deps.light_model = light(extract_=extract_, entail=entail)
    tag_of = {o.id: t for t, o in obs_of.items()}

    drafted, merged = [], []

    def spy_candidates(*a, **k):
        out = _REAL_CANDIDATES(*a, **k)
        # Snapshot: Merge later extends the first candidate's refs in place.
        drafted.extend(Drafted(c.id, tuple(c.refs)) for c in out)
        return out

    def spy_validate(*a, **k):
        c = k["c"] if "c" in k else a[3]
        merged.append(sorted(tag_of[r.observation_id] for r in c.refs))
        return _REAL_VALIDATE(*a, **k)

    monkeypatch.setattr(synth, "_candidates", spy_candidates)
    monkeypatch.setattr(synth, "_validate", spy_validate)
    scope = research.passage_scope()
    extraction = extract(deps, OWNER, RUN, list(scope), scope, plan=plan, research=research)
    bundle = synth.synthesize(deps, OWNER, RUN, "brief", plan, research, extraction)
    return Outcome(drafted=drafted, merged=merged,
                   items=[i for s in bundle.sections for i in s.items], tag_of=tag_of,
                   events=[(e.type, e.payload) for e in deps.repos.events.read_after(OWNER, RUN)])


def run(case: Case, make_deps, monkeypatch) -> Outcome:
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

    pages = {tag: (s.url, s.passage, s.published) for tag, s in (("a", case.a), ("b", case.b))}
    return synth_run(make_deps, monkeypatch, pages, draft, entail)


# ---- the five reviewed cases ----------------------------------------------------------------

@pytest.mark.parametrize("case", [P_TA_02, P_TA_03], ids=lambda c: c.id)
def test_temporal_observations_years_apart_stay_two_medium_items(case, make_deps, monkeypatch):
    # Before F4: one item supported by both Sources, HIGH.
    o = run(case, make_deps, monkeypatch)
    assert o.summary() == (2, [["a"], ["b"]], [(["a"], "MEDIUM"), (["b"], "MEDIUM")])
    assert all(i.as_of_period == "UNKNOWN" and i.temporally_ambiguous for i in o.items)
    assert len({i.id for i in o.items}) == 2


@pytest.mark.parametrize("case", [P_PS_01, P_PS_04], ids=lambda c: c.id)
def test_noncanonical_periods_no_longer_lose_the_second_fact(case, make_deps, monkeypatch):
    # Before F4: merged, and the literal check left only side a (side b's fact silently lost).
    o = run(case, make_deps, monkeypatch)
    assert o.summary() == (2, [["a"], ["b"]], [(["a"], "MEDIUM"), (["b"], "MEDIUM")])
    assert o.statements() == {case.a.statement, case.b.statement}


def test_same_name_entities_with_an_explicit_period_still_merge_and_lose_a_fact(
        make_deps, monkeypatch):
    """Retained limitation (not F4's rule): the period is explicit, so the key is unchanged.
    The literal check stops false corroboration ("London" is not in Passage b), but side b's
    fact is lost."""
    o = run(P_FM_02, make_deps, monkeypatch)
    assert o.summary() == (2, [["a", "b"]], [(["a"], "MEDIUM")])
    assert o.statements() == {P_FM_02.a.statement}


def test_same_name_entities_without_descriptors_are_still_falsely_corroborated(
        make_deps, monkeypatch):
    """Retained limitation: with no descriptor in the statements nothing deterministic tells
    the two companies apart, and each Passage does state the statement."""
    o = run(P_FM_02_BARE, make_deps, monkeypatch)
    assert o.summary() == (2, [["a", "b"]], [(["a", "b"], "HIGH")])


# ---- controls -------------------------------------------------------------------------------

def structured(statement, passages, period, value="310"):
    return {"statement": statement, "passages": passages, "entity": "Orrin Bio",
            "predicate": "employees", "value": value, "scope": "", "entities": ["Orrin Bio"],
            "as_of_period": period}


def supported(numbered, user):
    return [{"passage": n, "verdict": "SUPPORTED"} for n, _ in numbered]


TWO = {"a": ("https://a.example/x", "Orrin Bio has 310 employees in 2024.", None),
       "b": ("https://b.test/y", "Orrin Bio has 310 employees in 2024.", None)}


def test_duplicate_unknown_period_candidates_from_one_observation_still_deduplicate(
        make_deps, monkeypatch):
    page = {"a": ("https://a.example/x", "Orrin Bio has 310 employees.\n\n"
                  "Orrin Bio, which has 310 employees, is hiring.", None)}
    o = synth_run(make_deps, monkeypatch, page, lambda ps, u: [
        structured("Orrin Bio has 310 employees.", [1], "UNKNOWN"),
        structured("Orrin Bio has 310 employees.", [2], "UNKNOWN"),   # another Passage
        structured("Orrin Bio employs 310 people.", [1], "sometime")],  # unreadable period
        supported)
    assert o.summary() == (3, [["a", "a"]], [(["a", "a"], "MEDIUM")])
    assert len(o.items[0].supporting) == 2


def test_one_observation_found_by_two_subtasks_still_deduplicates(make_deps, monkeypatch):
    page = {"a": ("https://a.example/x", "Orrin Bio has 310 employees.", None)}
    o = synth_run(make_deps, monkeypatch, page,
                  lambda ps, u: [structured("Orrin Bio has 310 employees.", [1], "UNKNOWN")],
                  supported, found={"s1": ["a"], "s2": ["a"]})
    # Extraction reads a Source once (ADR-0006), so one candidate, not one per Sub-task.
    assert o.summary() == (1, [["a"]], [(["a"], "MEDIUM")])
    assert o.items[0].subtask_id == "s1"  # the more important Sub-task keeps it


def test_the_same_explicit_period_across_independent_observations_still_corroborates(
        make_deps, monkeypatch):
    o = synth_run(make_deps, monkeypatch, TWO, lambda ps, u: [
        structured("Orrin Bio has 310 employees in 2024.", [n], "2024") for n, _ in ps], supported)
    assert o.summary() == (2, [["a", "b"]], [(["a", "b"], "HIGH")])
    assert o.items[0].as_of_period == "2024" and o.logged("extraction_period_rejected") == []


def test_different_explicit_periods_stay_distinct(make_deps, monkeypatch):
    pages = {"a": ("https://a.example/x", "Orrin Bio had 310 employees in 2022.", None),
             "b": ("https://b.test/y", "Orrin Bio had 310 employees in 2024.", None)}
    o = synth_run(make_deps, monkeypatch, pages, lambda ps, u: [
        structured(t, [n], re.search(r"20\d\d", t).group(0)) for n, t in ps],
        lambda ps, u: [{"passage": n, "verdict": "SUPPORTED" if t in u.split("\n")[0]
                        else "INSUFFICIENT_EVIDENCE"} for n, t in ps])
    assert o.summary() == (2, [["a"], ["b"]], [(["a"], "MEDIUM"), (["b"], "MEDIUM")])
    assert sorted(i.as_of_period for i in o.items) == ["2022", "2024"]


def test_unstructured_candidates_are_still_keyed_by_statement_alone(make_deps, monkeypatch):
    """Out of F4's scope and unchanged: a candidate with no entity, predicate or value is keyed
    by its statement, so identical wording from two Observations still merges."""
    o = synth_run(make_deps, monkeypatch, TWO, lambda ps, u: [
        {"statement": "Orrin Bio has 310 employees in 2024.", "passages": [n]} for n, _ in ps],
        supported)
    assert o.summary() == (2, [["a", "b"]], [(["a", "b"], "HIGH")])


def test_a_proposal_citing_several_observations_becomes_one_fact_per_passage(
        make_deps, monkeypatch):
    """Since ADR-0006 a fact is anchored to the one Passage that states it, so a model's own
    grouping of several Sources into one candidate no longer exists (before: one item citing a
    and b, HIGH). With an UNKNOWN period F4's rule then applies to every fact: candidates merge
    only within the same Observation, so nothing here is corroborated across Sources."""
    three = {**TWO, "c": ("https://c.example/z", "Orrin Bio has 310 employees in 2024.", None)}
    o = synth_run(make_deps, monkeypatch, three, lambda ps, u: [
        structured("Orrin Bio has 310 employees.", [1, 2], "UNKNOWN"),
        structured("Orrin Bio employs 310 people.", [2, 1], "UNKNOWN"),
        structured("Orrin Bio has 310 employees.", [1], "UNKNOWN"),
        structured("Orrin Bio has 310 employees.", [3], "UNKNOWN")], supported)
    assert o.summary() == (5, [["a"], ["b"], ["c"]],
                           [(["a"], "MEDIUM"), (["b"], "MEDIUM"), (["c"], "MEDIUM")])


def test_candidate_ids_are_deterministic_and_carry_provenance_only_when_unknown(
        make_deps, monkeypatch):
    def ids(draft):
        o = synth_run(make_deps, monkeypatch, TWO, draft, supported)
        return [c.id for c in o.drafted]

    unknown = lambda ps, u: [structured("Orrin Bio has 310 employees.", [n], "UNKNOWN")  # noqa: E731
                             for n, _ in ps]
    first, again = ids(unknown), ids(unknown)
    assert first == again and first[0] != first[1]
    explicit = lambda ps, u: [structured("Orrin Bio has 310 employees in 2024.", [n], "2024")  # noqa: E731
                              for n, _ in ps]
    a, b = ids(explicit)
    assert a == b


# ---- a model-supplied period is checked before Merge (by Extraction, against its Passage) ----------------------------------------

UNDATED = {"a": ("https://a.example/x", "Orrin Bio has 310 employees.", None),
           "b": ("https://b.test/y", "Orrin Bio has 310 employees.", None)}


def test_the_same_invented_period_no_longer_merges_two_observations(make_deps, monkeypatch):
    # Neither cited text states 2024: the period is UNKNOWN before Merge, so the Observation
    # provenance rule keeps the two apart. Before: one item, HIGH.
    o = synth_run(make_deps, monkeypatch, UNDATED, lambda ps, u: [
        structured("Orrin Bio has 310 employees.", [n], "2024") for n, _ in ps], supported)
    assert o.summary() == (2, [["a"], ["b"]], [(["a"], "MEDIUM"), (["b"], "MEDIUM")])
    assert all(i.as_of_period == "UNKNOWN" for i in o.items)
    rejected = o.logged("extraction_period_rejected")
    assert [r["period"] for r in rejected] == ["2024", "2024"]
    assert len(o.drafted) == 2


def test_an_invented_period_is_dropped_only_where_the_cited_text_lacks_it(make_deps, monkeypatch):
    pages = {"a": ("https://a.example/x", "Orrin Bio has 310 employees in 2024.", None),
             "b": ("https://b.test/y", "Orrin Bio has 310 employees.", None)}
    o = synth_run(make_deps, monkeypatch, pages, lambda ps, u: [
        structured("Orrin Bio has 310 employees.", [n], "2024") for n, _ in ps], supported)
    assert o.summary() == (2, [["a"], ["b"]], [(["a"], "MEDIUM"), (["b"], "MEDIUM")])
    assert sorted(i.as_of_period for i in o.items) == ["2024", "UNKNOWN"]
    assert len(o.logged("extraction_period_rejected")) == 1


def test_a_period_stated_only_in_an_uncited_passage_does_not_count(make_deps, monkeypatch):
    page = {"a": ("https://a.example/x", "Orrin Bio was founded in 2024.\n\n"
                  "Orrin Bio has 310 employees.", None)}
    o = synth_run(make_deps, monkeypatch, page, lambda ps, u: [
        structured("Orrin Bio has 310 employees.", [2], "2024")], supported)
    (item,) = o.items
    assert item.as_of_period == "UNKNOWN"
    assert [r["period"] for r in o.logged("extraction_period_rejected")] == ["2024"]
