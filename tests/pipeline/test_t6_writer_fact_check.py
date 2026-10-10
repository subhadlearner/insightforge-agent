"""T6: Writer rules, Fact-Checker sampling and verdicts, and citation lookup."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from insightforge_agent.agents.web import FetchError, Page, PaywalledError
from insightforge_agent.domain.contracts import (
    Brief,
    Claim,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSection,
    PassageRef,
    ReportDraft,
    ReportSection,
)
from insightforge_agent.domain.models import RunState
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.pipeline.fact_check import fact_check, sample_positions, sample_size
from insightforge_agent.pipeline.graph import run_brief
from insightforge_agent.pipeline.write import violations
from insightforge_agent.stores.source_store import PassageDraft
from tests.pipeline import fakes
from tests.pipeline.test_happy_path import BRIEF

URL = "https://news.example.com/byd"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SALES = "BYD sold 4.27 million vehicles in 2025, up from 3.02 million in 2024."


class Fetcher:
    """Serves `pages` by URL; a value that is an exception class is raised instead."""

    def __init__(self, pages: dict) -> None:
        self.pages, self.fetched = pages, []

    def fetch(self, url: str) -> Page:
        self.fetched.append(url)
        page = self.pages[url]
        if isinstance(page, type) and issubclass(page, Exception):
            raise page(url)
        return Page(url=url, title="BYD sales", body=page)


def evidence(deps, statements, url=URL, body=None, kind="web"):
    """Store one Source whose Passages are `statements`, and an Evidence item per statement."""
    if kind == "pdf":
        obs = deps.store.ingest_document(
            owner_id="alice", run_id="run_1", content_hash="abc", filename="annual.pdf",
            chunks=[PassageDraft(s, page=1) for s in statements], fetched_at=T0,
            credibility_score=0.7)
    else:
        obs = deps.store.ingest_web(
            owner_id="alice", run_id="run_1", url=url, title="BYD sales",
            body=body or "\n\n".join(statements), fetched_at=T0, credibility_score=0.8,
            published_at=T0 - timedelta(days=30))
    items = [
        EvidenceItem(
            id=f"ev_{n}", subtask_id="s1", aspect_id="a1", statement=text, confidence="MEDIUM",
            supporting=[PassageRef(observation_id=obs.id, index=n)],
            primary=PassageRef(observation_id=obs.id, index=n))
        for n, text in enumerate(statements)
    ]
    return obs, EvidenceBundle(sections=[
        EvidenceSection(aspect_id="a1", heading="Sales", confidence="MEDIUM", items=items)])


def draft_of(texts, evidence_ids=None, historical=()):
    ids = evidence_ids or [f"ev_{n}" for n in range(len(texts))]
    return ReportDraft(title="T", sections=[ReportSection(heading="Sales", claims=[
        Claim(text=t, evidence_id=i, historical=n in historical)
        for n, (t, i) in enumerate(zip(texts, ids, strict=True))])])


# --- sampling -----------------------------------------------------------------------------

@pytest.mark.parametrize("claims, expected", [
    (1, 1), (4, 4), (5, 5), (24, 5), (25, 5), (26, 6), (30, 6), (100, 20), (101, 21)])
def test_sample_is_twenty_percent_with_a_minimum_of_five_and_all_when_fewer(claims, expected):
    assert sample_size(claims, 0.2, 5) == expected


def test_sample_is_seeded_from_the_run_id():
    a = sample_positions("run_a", 40, 0.2, 5)
    assert a == sample_positions("run_a", 40, 0.2, 5)
    assert len(a) == len(set(a)) == 8 and all(0 <= p < 40 for p in a)
    assert a != sample_positions("run_b", 40, 0.2, 5)


# --- verdicts -----------------------------------------------------------------------------

def test_matching_refetch_verifies_and_creates_a_new_observation(make_deps):
    fetcher = Fetcher({URL: SALES})
    deps = make_deps(fetcher=fetcher)
    obs, bundle = evidence(deps, [SALES])
    original = deps.repos.passages.list("alice", obs.id)
    result = fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle)
    (v,) = result.verdicts
    assert v.verdict == "verified" and v.check == "web_refetch" and v.similarity >= 0.85
    assert fetcher.fetched == [URL]
    seen = deps.repos.observations.list_for_source("alice", deps.repos.sources.list_for_run(
        "alice", "run_1")[0].id)
    assert len(seen) == 2 and v.matched.observation_id == seen[-1].id != obs.id
    assert v.original == bundle.sections[0].items[0].primary
    # The original Observation, its Passages and the Evidence item's citation are unchanged.
    assert deps.repos.passages.list("alice", obs.id) == original
    assert bundle.sections[0].items[0].primary.observation_id == obs.id
    assert result.draft == draft_of([SALES])


def test_changed_page_is_unverified_and_only_that_claim_is_marked(make_deps):
    other = "BYD announced a new factory in Hungary."
    deps = make_deps(fetcher=Fetcher({URL: SALES + "\n\nBYD exports grew."}))
    _, bundle = evidence(deps, [SALES, other], body=SALES + "\n\n" + other)
    result = fact_check(deps, "alice", "run_1", draft_of([SALES, other]), bundle)
    by_claim = {v.claim: v.verdict for v in result.verdicts}
    assert by_claim == {0: "verified", 1: "unverified"}
    texts = [c.text for c in result.draft.sections[0].claims]
    assert texts == [SALES, other + " [UNVERIFIED]"]


def test_numeric_claim_needs_its_number_in_the_matched_passage(make_deps):
    changed = SALES.replace("4.27", "4.28")
    deps = make_deps(fetcher=Fetcher({URL: changed}))
    _, bundle = evidence(deps, [SALES])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle).verdicts
    assert v.similarity >= 0.85  # close enough to match ...
    assert v.verdict == "unverified" and "4.27" in v.reason  # ... but the figure is gone


@pytest.mark.parametrize("failure", [PaywalledError, FetchError, TimeoutError])
def test_failed_refetch_is_unchecked_and_not_marked(make_deps, failure):
    deps = make_deps(fetcher=Fetcher({URL: failure}))
    _, bundle = evidence(deps, [SALES])
    result = fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle)
    assert [v.verdict for v in result.verdicts] == ["unchecked"]
    assert result.draft == draft_of([SALES])
    assert [e.type for e in deps.repos.events.read_after("alice", "run_1")
            if e.type == "fact_check_refetch_failed"]
    assert deps.repos.observations.list_for_source(
        "alice", deps.repos.sources.list_for_run("alice", "run_1")[0].id).__len__() == 1


def test_unchecked_claims_are_excluded_from_the_pass_rate(make_deps):
    good, gone = SALES, "Tesla cut prices by 6 percent in China."
    deps = make_deps(fetcher=Fetcher({URL: good, "https://b.example.com/t": FetchError}))
    _, b1 = evidence(deps, [good])
    _, b2 = evidence(deps, [gone], url="https://b.example.com/t")
    # Two more verified claims, one unverified, one unchecked.
    bundle = EvidenceBundle(sections=[
        EvidenceSection(aspect_id="a1", heading="x", confidence="MEDIUM", items=[
            i.model_copy(update={"id": f"ev_{n}"})
            for n, i in enumerate(b1.sections[0].items + b2.sections[0].items)])])
    result = fact_check(deps, "alice", "run_1", draft_of([good, gone]), bundle)
    s = result.summary
    assert (s["verified"], s["unverified"], s["unchecked"]) == (1, 0, 1)
    assert s["pass_rate"] == 1.0 and s["claims"] == 2 and s["sampled"] == 2


def test_pass_rate_is_none_when_nothing_could_be_checked(make_deps):
    deps = make_deps(fetcher=Fetcher({URL: FetchError}))
    _, bundle = evidence(deps, [SALES])
    assert fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle).summary["pass_rate"] is None


def test_a_source_cited_by_several_claims_is_fetched_once(make_deps):
    a, b = SALES, "Plug-in hybrids made up about half of those sales."
    fetcher = Fetcher({URL: a + "\n\n" + b})
    deps = make_deps(fetcher=fetcher)
    _, bundle = evidence(deps, [a, b])
    result = fact_check(deps, "alice", "run_1", draft_of([a, b]), bundle)
    assert [v.verdict for v in result.verdicts] == ["verified", "verified"]
    assert fetcher.fetched == [URL]


def test_only_the_sample_is_checked(make_deps):
    texts = [f"Fact number {n} about BYD output in plant {n}." for n in range(30)]
    fetcher = Fetcher({URL: "\n\n".join(texts)})
    deps = make_deps(fetcher=fetcher)
    _, bundle = evidence(deps, texts)
    result = fact_check(deps, "alice", "run_x", draft_of(texts), bundle)
    assert len(result.verdicts) == 6 and result.summary["claims"] == 30
    assert [v.claim for v in result.verdicts] == sample_positions("run_x", 30, 0.2, 5)


def test_document_claims_are_checked_against_the_stored_chunks(make_deps):
    fetcher = Fetcher({})
    deps = make_deps(fetcher=fetcher)
    _, bundle = evidence(deps, [SALES, "Unrelated chunk."], kind="pdf")
    (v,) = fact_check(deps, "alice", "run_1", draft_of([SALES]), bundle).verdicts
    assert v.verdict == "verified" and v.check == "document_passages" and fetcher.fetched == []


def test_historical_claims_are_sampled_but_unchecked_for_now(make_deps):
    deps = make_deps(fetcher=Fetcher({URL: SALES}))
    _, bundle = evidence(deps, [SALES])
    (v,) = fact_check(deps, "alice", "run_1", draft_of([SALES], historical={0}), bundle).verdicts
    assert v.verdict == "unchecked" and v.check == "not_checkable"


# --- citations ----------------------------------------------------------------------------

def test_citation_lookup_covers_every_claim_even_unsampled(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    report = deps.repos.reports.get_for_run("alice", run.id)
    claims = [c for s in report.body["draft"]["sections"] for c in s["claims"]]
    citations = report_citations(deps.store, "alice", report)
    assert len(citations) == len(claims)
    assert report.body["verified"]["summary"]["sampled"] <= len(claims)
    sources = {s.id: s for s in deps.repos.sources.list_for_run("alice", run.id)}
    for cit, claim in zip(citations, claims, strict=True):
        src = sources[cit.source_id]
        assert cit.evidence_id == claim["evidence_id"]
        assert cit.source_title == src.title and cit.credibility_score == src.credibility_score
        assert cit.published_at == src.published_at
        assert cit.passage_text


def test_citation_shows_the_primary_passage_text(make_deps):
    deps = make_deps()
    obs, bundle = evidence(deps, ["First paragraph.", SALES])
    ref = bundle.sections[0].items[1].primary
    cit = deps.store.citation("alice", ref)
    assert cit.passage_text == SALES and cit.source_title == "BYD sales"
    assert cit.credibility_score == 0.8 and cit.published_at == T0 - timedelta(days=30)


# --- Writer -------------------------------------------------------------------------------

def bundle_with(statement, entity=""):
    item = EvidenceItem(
        id="ev_1", subtask_id="s", aspect_id="a", statement=statement, confidence="HIGH",
        supporting=[PassageRef(observation_id="o", index=0)],
        primary=PassageRef(observation_id="o", index=0), entity=entity)
    return EvidenceBundle(sections=[
        EvidenceSection(aspect_id="a", heading="H", confidence="HIGH", items=[item])])


def test_writer_rejects_a_name_the_evidence_does_not_contain():
    bundle = bundle_with("BYD sold 4.27 million vehicles in 2025.")
    ok = violations(draft_of(["BYD sold 4.27 million vehicles in 2025."], ["ev_1"]), bundle)
    bad = violations(draft_of(["Sales were strong, said Tesla."], ["ev_1"]), bundle)
    assert ok == [] and len(bad) == 1 and "Tesla" in bad[0]


def test_writer_allows_a_possessive_of_a_name_in_the_evidence():
    bundle = bundle_with("The company expanded to 70 countries.", entity="BYD")
    assert violations(draft_of(["The firm grew. BYD's network spans 70 countries."],
                               ["ev_1"]), bundle) == []


def test_writer_retry_lists_the_offending_claims(make_deps):
    writer = fakes.writer(bad_first=1)
    deps = make_deps(writer_model=writer)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    retry_prompt = str(writer.seen[1][-1].content)
    assert "broke these rules" in retry_prompt and "ev_nope" in retry_prompt


# --- the Run ------------------------------------------------------------------------------

def test_run_stores_the_fact_check_summary_with_the_report(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    body = deps.repos.reports.get_for_run("alice", run.id).body
    summary = body["verified"]["summary"]
    assert summary["implemented"] is True and summary["sampled"] == len(body["verified"]["verdicts"])
    assert summary["sampled"] >= 1 and summary["similarity_threshold"] == 0.85
    json.dumps(body)  # storable as is
    assert any(e.type == "fact_check_summary" for e in deps.repos.events.read_after("alice", run.id))


def test_run_whose_refetch_fails_still_completes_with_unchecked_claims(make_deps):
    search = fakes.FixtureSearch()

    class Flaky:
        def __init__(self):
            self.fail = False

        def search(self, query, limit):
            return search.search(query, limit)

        def fetch(self, url):
            if self.fail:  # research fetches succeed, fact-check re-fetches do not
                raise FetchError(url)
            return search.fetch(url)

    flaky = Flaky()
    deps = make_deps(search=flaky, fetcher=flaky)
    def on_state(snapshot):
        flaky.fail = flaky.fail or "draft" in snapshot

    run = run_brief(deps, "alice", Brief(text=BRIEF), on_state=on_state)
    assert run.state == RunState.COMPLETE
    summary = deps.repos.reports.get_for_run("alice", run.id).body["verified"]["summary"]
    assert summary["unchecked"] == summary["sampled"] > 0 and summary["pass_rate"] is None
