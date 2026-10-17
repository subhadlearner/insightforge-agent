"""`insightforge-agent eval` end to end (T16 #17): every brief runs through the production
`run_brief` with scripted models (tests/evaluation/world.py), and every assertion reads the
report files the command wrote, checked against the Runs and Reports it persisted.

The provider names label scripted runs here; no provider is called."""

import copy
import itertools
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from insightforge_agent.domain.contracts import ReportDraft
from insightforge_agent.domain.evidence import Thresholds
from insightforge_agent.domain.models import RunState
from insightforge_agent.evaluation.cli import main
from insightforge_agent.evaluation.judge import CRITERIA, RUBRIC_VERSION
from insightforge_agent.evaluation.library import load_library
from insightforge_agent.pipeline.citations import report_citations
from insightforge_agent.stores.source_store import SourceStore
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.evaluation.world import World
from tests.scripted import ScriptedChat

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "samples" / "briefs"
SHEET = json.dumps({**{c: 4 for c in CRITERIA}, "rationale": "Organised."})


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    """Settings for every provider validate; the scripted world never uses the keys."""
    for key in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.setenv(key, "test-key-not-used")
    monkeypatch.setenv("SEARCH_PROVIDERS", "tavily")


class TunedChat(ScriptedChat):
    """A scripted judge that carries generation settings, as a real chat model does."""

    temperature: float = 0.0
    max_tokens: int = 16000


class Judges:
    """The judge factory: records the Settings it was built from."""

    def __init__(self, reply: str = SHEET) -> None:
        self.reply, self.built, self.models = reply, [], []

    def __call__(self, settings):
        self.built.append(settings)
        self.models.append(TunedChat(reply=lambda messages: AIMessage(content=self.reply)))
        return self.models[-1]


def evaluate(tmp_path, *args, world=None, judges=None) -> tuple[int, dict, Path]:
    world, judges = world or World(), judges or Judges()
    out = tmp_path / "out"
    code = main(["--library", str(LIBRARY), "--out", str(out), *args],
                make_deps=world.make_deps, make_judge=judges, root=ROOT)
    if code != 0:
        return code, {}, out
    [run_dir] = list(out.iterdir())
    return code, json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8")), run_dir


def provider(report: dict, name: str) -> dict:
    return next(p for p in report["providers"] if p["provider"] == name)


def samples(result: dict) -> dict[str, dict]:
    return {s["sample_id"]: s for s in result["samples"]}


def stored(result: dict) -> SqliteRepos:
    return SqliteRepos(str(Path(result["workdir"]) / "eval.db"))


def events(repos, owner, run_id, type_):
    out, cursor = [], 0
    while batch := repos.events.read_after(owner, run_id, cursor):
        out += [e for e in batch if e.type == type_]
        cursor = batch[-1].seq
    return out


# --- the whole library ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    """All five briefs, once, labelled anthropic so the §11 rule applies."""
    tmp = tmp_path_factory.mktemp("full")
    mp = pytest.MonkeyPatch()
    for key in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY"):
        mp.setenv(key, "test-key-not-used")
    mp.setenv("SEARCH_PROVIDERS", "tavily")
    mp.chdir(tmp)
    judges, world = Judges(), World()
    judges.world = world  # the scripted models record what each Run's Planner was given
    try:
        code, report, run_dir = evaluate(tmp, "--provider", "anthropic", "--judge-provider",
                                         "groq", world=world, judges=judges)
    finally:
        mp.undo()
    return code, report, run_dir, judges


def test_every_sample_brief_runs_through_the_pipeline_and_completes(full_run):
    code, report, _, _ = full_run
    assert code == 0
    result = provider(report, "anthropic")
    assert len(result["samples"]) == 5
    assert {s["status"] for s in result["samples"]} == {"complete"}
    repos = stored(result)
    for s in result["samples"]:
        run = repos.runs.get(s["owner_id"], s["run_id"])
        assert run.state == RunState.COMPLETE
        assert repos.reports.get_for_run(s["owner_id"], s["run_id"]).id == s["report_id"]


def test_fact_check_figures_are_the_persisted_verdicts(full_run):
    _, report, _, _ = full_run
    result = provider(report, "anthropic")
    repos = stored(result)
    total = Counter()
    for s in result["samples"]:
        body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
        counted = Counter(v["verdict"] for v in body["verified"]["verdicts"])
        total += counted
        fc = s["fact_check"]
        assert (fc["verified"], fc["unverified"], fc["unchecked"]) == (
            counted["verified"], counted["unverified"], counted["unchecked"])
        assert fc["sampled"] == len(body["verified"]["verdicts"]) >= 1
    totals = result["totals"]["fact_check"]
    assert totals["verified"] == total["verified"]
    assert totals["checked"] == total["verified"] + total["unverified"]


def test_citation_accuracy_covers_every_claim_of_every_report(full_run):
    _, report, _, _ = full_run
    result = provider(report, "anthropic")
    repos = stored(result)
    for s in result["samples"]:
        body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
        claims = sum(len(sec["claims"]) for sec in body["draft"]["sections"])
        assert s["citations"]["claims"] == claims
        assert sum(k["claims"] for k in s["citations"]["by_kind"].values()) == claims
    assert result["totals"]["citations"]["claims"] == sum(
        s["citations"]["claims"] for s in result["samples"])


# What tells the samples apart, as each Brief states it. The scripted research content is
# shared fixture material, so these tests show that each sample's own scope reaches the
# Planner and the judge; research quality on real content is T18's to judge.
DISTINCT = {
    "competitive-landscape-cloud": ("Amazon Web Services", "Microsoft Azure", "Google Cloud"),
    "regulatory-impact-semiconductors": ("export controls", "Nvidia", "AMD"),
    "market-sizing-solar": ("newly installed", "outlook for 2026", "forecasts"),
    "document-annual-report": ("Northwind Health", "annual report"),
    "memory-prior-report": ("earlier report", "last six months"),
}
ALLOWED = {"document-annual-report": "web, documents", "memory-prior-report": "web, memory"}


def human_texts(model) -> list[str]:
    return list(dict.fromkeys(str(m.content) for conv in model.seen for m in conv
                              if isinstance(m, HumanMessage)))


def test_each_samples_own_brief_and_sources_reach_the_planner(full_run):
    _, _, _, judges = full_run
    plans = [t for t in human_texts(judges.world.built["anthropic"].planner_model)
             if t.startswith("PLAN.")]
    for sample in load_library(LIBRARY, ROOT):
        [plan] = [t for t in plans if f"Brief: {sample.brief}" in t]
        assert f"Allowed source types: {ALLOWED.get(sample.id, 'web')}." in plan, sample.id
        assert all(term in plan for term in DISTINCT[sample.id]), sample.id
        brief_part = plan.split("\nEarlier Runs")[0]  # summaries may mention the setup Run
        assert not any(DISTINCT[o][0] in brief_part for o in DISTINCT if o != sample.id)
    # The memory follow-up plans with its setup Run's summary, never another sample's.
    [follow_up] = [t for t in plans if "Update the earlier briefing on BYD" in t]
    assert "Earlier Runs" in follow_up and "Amazon Web Services" not in follow_up


def test_each_samples_brief_and_expected_structure_reach_the_judge(full_run):
    _, _, _, judges = full_run
    [judge] = judges.models
    prompts = human_texts(judge)
    assert len(prompts) == 5
    for sample in load_library(LIBRARY, ROOT):
        [prompt] = [p for p in prompts if f"Brief: {sample.brief}" in p]
        for expected in sample.expected_report_structure.sections:
            assert f"- {expected.heading}" in prompt, (sample.id, expected.heading)
            assert all(item in prompt for item in expected.must_cite), sample.id


# --- domain coverage --------------------------------------------------------------------------
# The briefs are about different industries, so their offline Runs must research their own
# subject: a cloud Brief that completes on EV pages proves nothing. These checks read what the
# production pipeline persisted (the plan, Sources, extracted facts, the Run summary, the
# Evidence bundle and the citations `report_citations` resolves). The contaminant is the
# EV material other tests use, read from its own fixture. Scripted models copy Passage
# sentences into Claims, so nothing here shows a model understood its sources: that is T18.

EV_FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "search" / "ev_market.json")
                        .read_text(encoding="utf-8"))
EV_URLS = set(EV_FIXTURE["pages"])
EV_TERMS = ("BYD", "Tesla", "electric vehicle", "EV ", "LFP", "battery", "Orrin")
DOMAIN_TERMS = {  # what each Brief is about, in its own words
    "competitive-landscape-cloud": ("Amazon", "Azure", "Google Cloud", "Microsoft", "Alphabet",
                                    "cloud"),
    "regulatory-impact-semiconductors": ("export", "Nvidia", "AMD", "chip"),
    "market-sizing-solar": ("solar",),
    "document-annual-report": ("Northwind", "NW-200", "medical devices", "Cardiology"),
}
_OPENERS = {"The", "In", "A", "An", "Its", "This", "New"}


def names_in(text: str) -> set[str]:
    return set(re.findall(r"\b[A-Z][A-Za-z0-9-]+", text)) - _OPENERS


def mentions(text: str, terms) -> bool:
    return any(t.casefold() in text.casefold() for t in terms)


def domain_problems(repos, sample: dict, report) -> list[str]:
    """Where a sample's persisted Run strays from its own subject, or cites a Passage that does
    not state what its Claim attributes."""
    owner, run_id, sid = sample["owner_id"], sample["run_id"], sample["sample_id"]
    terms, problems = DOMAIN_TERMS[sid], []
    brief = next(x for x in load_library(LIBRARY, ROOT) if x.id == sid)
    expected = {s.heading for s in brief.expected_report_structure.sections}
    documents = {Path(d).name for d in brief.documents}
    for record in repos.subtasks.list(owner, run_id):
        query = record.subtask.query
        if not mentions(query, terms) or mentions(query, EV_TERMS):
            problems.append(f"Sub-task query off subject: {query!r}")
    headings = [s["heading"] for s in report.body["evidence"]["sections"]]
    if len(set(headings) & expected) < 3 or not set(headings) <= expected:
        problems.append(f"Evidence headings {headings} do not cover the expected Aspects")
    sources = {s.id: s for s in repos.sources.list_for_run(owner, run_id)}
    for s in sources.values():
        if s.locator in EV_URLS or mentions(s.title, EV_TERMS):
            problems.append(f"another industry's Source was ingested: {s.locator}")
    for fact in repos.entities.list_for_run(owner, run_id):
        if mentions(" ".join(fact.entities), EV_TERMS):
            problems.append(f"another industry's entity was extracted: {fact.entities}")
    summary = repos.summaries.get_for_run(owner, run_id)
    if mentions(" ".join(summary.key_entities), EV_TERMS):
        problems.append(f"the Run summary names another industry: {summary.key_entities}")
    store = SourceStore(repos.sources, repos.observations, repos.passages)
    for c in report_citations(store, owner, report):
        claim = ReportDraft.model_validate(report.body["draft"]).sections[c.section] \
            .claims[c.claim].text
        where = f"claim {c.section}.{c.claim}"
        if c.source_id not in sources:
            problems.append(f"{where} cites a Source this Run did not fetch")
        if mentions(claim, EV_TERMS) or mentions(c.passage_text, EV_TERMS):
            problems.append(f"{where} rests on another industry: {claim!r}")
        source = sources.get(c.source_id)
        if source is not None and source.kind == "pdf":
            if source.title not in documents:
                problems.append(f"{where} cites a document this brief did not upload")
        elif not mentions(c.passage_text, terms):
            problems.append(f"{where} cites a Passage off subject: {c.passage_text!r}")
        unstated = names_in(claim) - names_in(c.passage_text)
        if unstated:
            problems.append(f"{where} attributes {sorted(unstated)} to a Passage that does "
                            f"not name them")
    return problems


def stored_report(result: dict, sid: str):
    s = samples(result)[sid]
    repos = stored(result)
    return repos, s, repos.reports.get_for_run(s["owner_id"], s["run_id"])


@pytest.mark.parametrize("sid", sorted(DOMAIN_TERMS))
def test_each_non_ev_brief_researches_and_cites_its_own_subject(full_run, sid):
    _, report, _, _ = full_run
    repos, s, stored_ = stored_report(provider(report, "anthropic"), sid)
    assert s["status"] == "complete"
    assert domain_problems(repos, s, stored_) == []
    assert s["citations"]["claims"] >= 3 and s["citations"]["accuracy"] == 1.0
    assert s["fact_check"]["checked"] >= 1


def test_every_brief_cites_only_its_own_runs_sources_and_no_two_briefs_share_one(full_run):
    _, report, _, _ = full_run
    result = provider(report, "anthropic")
    repos = stored(result)
    store = SourceStore(repos.sources, repos.observations, repos.passages)
    cited: dict[str, set[str]] = {}
    for s in result["samples"]:
        body = repos.reports.get_for_run(s["owner_id"], s["run_id"])
        cited[s["sample_id"]] = {store.source_for_passage(s["owner_id"], c.passage).locator
                                 for c in report_citations(store, s["owner_id"], body)}
    for a, b in itertools.combinations(cited, 2):
        assert not cited[a] & cited[b], (a, b)
    assert cited["memory-prior-report"] & EV_URLS  # the EV brief does research EV pages


def test_a_brief_whose_search_returns_another_industrys_pages_completes_but_fails_coverage(
        tmp_path):
    """Completion alone would pass this Run; the domain checks do not."""
    world = World(substitute={"AWS Azure Google Cloud revenue growth 2025":
                              "BYD battery electric vehicle sales 2025"})
    code, report, _ = evaluate(tmp_path, "--provider", "gemini", "--judge-provider", "none",
                               "--brief", "competitive-landscape-cloud", world=world)
    repos, s, stored_ = stored_report(provider(report, "gemini"), "competitive-landscape-cloud")
    assert code == 0 and s["status"] == "complete"
    problems = domain_problems(repos, s, stored_)
    assert any("another industry's Source" in p for p in problems)
    assert any("rests on another industry" in p for p in problems)


def test_a_claim_cited_to_another_companys_passage_fails_attribution(full_run):
    """Swap the primary Passages of two Evidence items about different companies: every link
    still resolves, but each Claim now names a company its cited Passage does not."""
    _, report, _, _ = full_run
    repos, s, stored_ = stored_report(provider(report, "anthropic"),
                                      "competitive-landscape-cloud")
    body = copy.deepcopy(stored_.body)
    items = [i for sec in body["evidence"]["sections"] for i in sec["items"]]
    aws = next(i for i in items if i["statement"].startswith("Amazon Web Services revenue"))
    azure = next(i for i in items if i["statement"].startswith("Microsoft Azure revenue"))
    aws["primary"], azure["primary"] = azure["primary"], aws["primary"]
    problems = domain_problems(repos, s, stored_.model_copy(update={"body": body}))
    assert any("attributes" in p and "Amazon" in p for p in problems)
    assert any("attributes" in p and "Microsoft" in p for p in problems)


def test_the_document_brief_cites_and_checks_the_uploaded_pdf(full_run):
    _, report, _, _ = full_run
    s = samples(provider(report, "anthropic"))["document-annual-report"]
    assert s["citations"]["by_kind"]["document"]["claims"] > 0
    body = stored(provider(report, "anthropic")).reports.get_for_run(
        s["owner_id"], s["run_id"]).body
    assert "document_passages" in {v["check"] for v in body["verified"]["verdicts"]}


def test_the_memory_brief_runs_its_setup_first_and_cites_history_apart_from_evidence(full_run):
    _, report, _, _ = full_run
    result = provider(report, "anthropic")
    s = samples(result)["memory-prior-report"]
    repos = stored(result)
    [setup_id] = s["setup_run_ids"]
    setup = repos.runs.get(s["owner_id"], setup_id)
    assert setup.state == RunState.COMPLETE and setup.created_at <= repos.runs.get(
        s["owner_id"], s["run_id"]).created_at
    body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
    historical_ids = {h["id"] for h in body["historical"]["items"]}
    evidence_ids = {i["id"] for sec in body["evidence"]["sections"] for i in sec["items"]}
    cited = [c for sec in body["draft"]["sections"] for c in sec["claims"] if c["historical"]]
    assert cited and all(c["evidence_id"].startswith("hist_") for c in cited)
    assert historical_ids and not historical_ids & evidence_ids
    assert s["citations"]["by_kind"]["historical"]["claims"] == len(cited)
    assert events(repos, s["owner_id"], s["run_id"], "memory_search")
    # Memory is per sample: no other sample's User has the setup Run.
    others = [x for x in result["samples"] if x["sample_id"] != "memory-prior-report"]
    assert all(x["owner_id"] != s["owner_id"] for x in others)


def test_provider_and_judge_identity_are_recorded_from_what_ran(full_run):
    _, report, _, judges = full_run
    result = provider(report, "anthropic")
    assert result["configured_models"] == {"planner": "claude-haiku-5-5",
                                           "writer": "claude-haiku-5-5",
                                           "light": "claude-haiku-5-5"}
    # The models that actually ran here are scripted, and the report says so.
    assert set(result["runtime_models"].values()) == {"ScriptedChat"}
    assert result["runtime_services"] == {"embedder": "HashingEmbedder", "search": "RecordedPages",
                                          "fetcher": "RecordedPages"}
    assert result["conditions"]["configured_embedding_model"] == "BAAI/bge-small-en-v1.5"
    assert (result["conditions"]["fact_check_rate"], result["conditions"]["clarification"]) == (
        0.2, "off")
    assert [s.llm_provider for s in judges.built] == ["groq"]
    assert report["judge"] == {"provider": "groq", "model": "openai/gpt-oss-120b",
                               "runtime_model": "TunedChat", "role": "writer",
                               "inference": {"temperature": 0.0, "max_tokens": 16000},
                               "rubric_version": RUBRIC_VERSION}
    assert {s["synthesis"]["status"] for s in result["samples"]} == {"judged"}
    assert result["totals"]["synthesis"]["mean_score"] == 4.0


def test_the_full_library_at_or_above_the_threshold_needs_no_remediation(full_run):
    _, report, _, _ = full_run
    result = provider(report, "anthropic")
    assert result["totals"]["fact_check"]["pass_rate"] >= 0.8
    assert result["remediation"]["status"] == "not_required"
    assert report["library"]["briefs_in_library"] == 5 and report["comparability"] == {}


def test_the_markdown_report_is_written_beside_the_json(full_run):
    _, report, run_dir, _ = full_run
    md = (run_dir / "eval-report.md").read_text(encoding="utf-8")
    assert "## Provider: anthropic" in md and "Remediation (section 11): **not_required**" in md
    assert "Fact-check sampling: 20% of Claims, at least 5, seeded by the Run" in md
    for s in provider(report, "anthropic")["samples"]:
        assert f"| {s['sample_id']} | complete |" in md
    assert "not that the Passage semantically supports the Claim" in md


# --- per provider, failures and comparability ------------------------------------------------

TWO = ("--brief", "market-sizing-solar", "--brief", "competitive-landscape-cloud")


def test_providers_are_recorded_separately_and_a_failed_run_is_not_measured(tmp_path):
    world = World(fail_all={"anthropic": ("solar photovoltaic",)})
    code, report, _ = evaluate(tmp_path, "--provider", "anthropic", "--provider", "groq",
                               "--judge-provider", "gemini", *TWO, world=world)
    assert code == 0
    anthropic, groq = provider(report, "anthropic"), provider(report, "groq")
    assert anthropic["workdir"] != groq["workdir"]
    failed = samples(anthropic)["market-sizing-solar"]
    assert failed["status"] == "failed" and "failed" in failed["reason"]
    assert failed["fact_check"] is None and failed["citations"] is None
    assert failed["synthesis"]["status"] == "not_judged"
    # The failed Run exists in anthropic's store, with no Report; groq's Run of it completed.
    a_repos = stored(anthropic)
    assert a_repos.runs.get(failed["owner_id"], failed["run_id"]).state == RunState.FAILED
    assert samples(groq)["market-sizing-solar"]["status"] == "complete"
    assert failed["run_id"] not in {r.id for r in stored(groq).runs.list_for_owner(
        failed["owner_id"])}
    assert (anthropic["totals"]["complete"], groq["totals"]["complete"]) == (1, 2)
    # The pooled figures cover only measured Reports: never a zero for the failed one.
    assert anthropic["totals"]["fact_check"]["sampled"] == \
        samples(anthropic)["competitive-landscape-cloud"]["fact_check"]["sampled"]
    assert anthropic["remediation"]["status"] == "undetermined"
    assert groq["remediation"]["status"] == "not_applicable"
    for metric in ("fact_check", "citations", "synthesis"):
        c = report["comparability"][metric]
        assert c["comparable"] is False, metric
        assert "anthropic has none for market-sizing-solar" in c["reason"], metric
        assert "groq has none" not in c["reason"], metric


def test_providers_measured_on_the_same_briefs_are_comparable(tmp_path):
    code, report, _ = evaluate(tmp_path, "--provider", "gemini", "--provider", "groq",
                               "--judge-provider", "gemini", "--brief",
                               "competitive-landscape-cloud")
    assert code == 0
    assert all(c["comparable"] for c in report["comparability"].values())


def test_a_run_that_loses_a_subtask_completes_and_is_measured_with_its_gap(tmp_path):
    world = World(fail_one={"gemini": ("export controls",)})
    code, report, _ = evaluate(tmp_path, "--provider", "gemini", "--judge-provider", "groq",
                               "--brief", "regulatory-impact-semiconductors", world=world)
    s = samples(provider(report, "gemini"))["regulatory-impact-semiconductors"]
    assert code == 0 and s["status"] == "complete" and s["fact_check"]["sampled"] >= 1
    body = stored(provider(report, "gemini")).reports.get_for_run(s["owner_id"],
                                                                   s["run_id"]).body
    assert body["draft"]["gaps"]


def test_a_failed_setup_run_leaves_the_memory_brief_unrun(tmp_path):
    world = World(fail_all={"gemini": ("solid-state battery",)})  # in the setup Brief only
    code, report, _ = evaluate(tmp_path, "--provider", "gemini", "--judge-provider", "none",
                               "--brief", "memory-prior-report", world=world)
    result = provider(report, "gemini")
    s = samples(result)["memory-prior-report"]
    assert code == 0 and s["status"] == "setup_failed" and s["run_id"] is None
    assert len(stored(result).runs.list_for_owner(s["owner_id"])) == 1  # the setup Run only
    assert s["fact_check"] is None and result["totals"]["fact_check"]["pass_rate"] is None


def test_below_the_threshold_the_full_anthropic_run_requires_remediation(tmp_path):
    world = World(drift=frozenset({"anthropic"}))
    code, report, _ = evaluate(tmp_path, "--provider", "anthropic", "--judge-provider", "none",
                               world=world)
    anthropic = provider(report, "anthropic")
    assert code == 0 and anthropic["totals"]["complete"] == 5
    assert anthropic["totals"]["fact_check"]["pass_rate"] < 0.8
    assert anthropic["remediation"]["status"] == "required"


def test_a_full_anthropic_run_with_a_failed_brief_below_the_threshold_is_undetermined(tmp_path):
    world = World(drift=frozenset({"anthropic"}),
                  fail_all={"anthropic": ("solar photovoltaic",)})
    code, report, _ = evaluate(tmp_path, "--provider", "anthropic", "--judge-provider", "none",
                               world=world)
    anthropic = provider(report, "anthropic")
    assert code == 0 and anthropic["totals"]["complete"] == 4
    assert anthropic["totals"]["fact_check"]["pass_rate"] < 0.8
    assert anthropic["remediation"]["status"] == "undetermined"
    assert "below 80%" in anthropic["remediation"]["reason"]
    assert "not measured: market-sizing-solar" in anthropic["remediation"]["reason"]


def test_a_partial_anthropic_run_below_the_threshold_is_undetermined_and_says_so(tmp_path):
    world = World(drift=frozenset({"anthropic"}))
    code, report, _ = evaluate(tmp_path, "--provider", "anthropic", "--provider", "gemini",
                               "--judge-provider", "none", "--brief",
                               "competitive-landscape-cloud", world=world)
    anthropic, gemini = provider(report, "anthropic"), provider(report, "gemini")
    assert code == 0
    assert anthropic["totals"]["fact_check"]["pass_rate"] < 0.8
    assert anthropic["remediation"]["status"] == "undetermined"
    assert "partial run" in anthropic["remediation"]["reason"]
    assert "below 80%" in anthropic["remediation"]["reason"]
    assert gemini["totals"]["fact_check"]["pass_rate"] >= 0.8  # the same world without drift
    assert gemini["remediation"]["status"] == "not_applicable"


def test_providers_run_under_different_conditions_are_never_comparable(tmp_path):
    world = World(overrides={"groq": {"fact_check_rate": 0.5}})
    code, report, run_dir = evaluate(tmp_path, "--provider", "gemini", "--provider", "groq",
                                     "--judge-provider", "none", "--brief",
                                     "competitive-landscape-cloud", world=world)
    assert code == 0
    assert provider(report, "groq")["conditions"]["fact_check_rate"] == 0.5
    for metric, c in report["comparability"].items():
        assert c["comparable"] is False and "fact_check_rate" in c["reason"], metric
    # Each provider's sampling is described in its own section, never one for all.
    md = (run_dir / "eval-report.md").read_text(encoding="utf-8")
    gemini_md, groq_md = md.split("## Provider: groq")
    assert "Fact-check sampling: 20% of Claims" in gemini_md.split("## Provider: gemini")[1]
    assert "Fact-check sampling: 50% of Claims" in groq_md.split("## Provider")[0]
    definitions = md.split("## Metric definitions")[1]
    assert "20%" not in definitions and "50%" not in definitions


def test_different_evidence_thresholds_are_recorded_and_never_comparable(tmp_path):
    world = World(overrides={"groq": {"thresholds": Thresholds(credibility_high=0.9,
                                                                recency_fresh_days=30)}})
    code, report, _ = evaluate(tmp_path, "--provider", "gemini", "--provider", "groq",
                               "--judge-provider", "none", "--brief",
                               "competitive-landscape-cloud", world=world)
    groq, gemini = provider(report, "groq")["conditions"], provider(report, "gemini")["conditions"]
    assert code == 0
    assert (groq["credibility_high"], groq["recency_fresh_days"]) == (0.9, 30)
    assert (gemini["credibility_high"], gemini["credibility_medium"],
            gemini["recency_fresh_days"], gemini["recency_recent_days"]) == (0.65, 0.4, 365, 1095)
    for metric, c in report["comparability"].items():
        assert c["comparable"] is False, metric
        assert "credibility_high" in c["reason"] and "recency_fresh_days" in c["reason"], metric


class UnreadableReports(World):
    """Once a Run whose Brief holds `keyword` is COMPLETE, reading its Report fails (a store
    fault on the recovery path; the pipeline itself still stores the Report)."""

    def __init__(self, keyword: str) -> None:
        super().__init__()
        self.keyword = keyword

    def make_deps(self, settings, repos, checkpointer, qdrant):
        deps = super().make_deps(settings, repos, checkpointer, qdrant)
        inner, runs, keyword = repos.reports, repos.runs, self.keyword

        class Reports:
            def __getattr__(self, name):
                return getattr(inner, name)

            def get_for_run(self, owner_id, run_id):
                run = runs.get(owner_id, run_id)
                if run.state == RunState.COMPLETE and keyword in run.brief:
                    raise OSError("disk I/O error")
                return inner.get_for_run(owner_id, run_id)
        repos.reports = Reports()
        return deps


def test_a_completed_run_whose_report_cannot_be_read_is_an_error_and_the_rest_is_written(
        tmp_path):
    code, report, run_dir = evaluate(tmp_path, "--provider", "gemini", "--judge-provider",
                                     "none", *TWO, world=UnreadableReports("solar"))
    result = provider(report, "gemini")
    bad, good = samples(result)["market-sizing-solar"], samples(result)["competitive-landscape-cloud"]
    assert code == 0 and (run_dir / "eval-report.md").exists()
    assert bad["status"] == "error" and bad["run_id"] and bad["report_id"] is None
    assert "Report could not be read" in bad["reason"] and "disk I/O error" in bad["reason"]
    assert bad["fact_check"] is None and bad["synthesis"]["status"] == "not_judged"
    assert good["status"] == "complete" and good["fact_check"]["pass_rate"] is not None


# --- the judge ------------------------------------------------------------------------------


def test_a_failing_judge_leaves_the_deterministic_metrics_untouched(tmp_path):
    code, report, run_dir = evaluate(tmp_path, "--provider", "gemini", "--judge-provider",
                                     "groq", "--brief", "competitive-landscape-cloud",
                                     judges=Judges(reply="Looks fine to me."))
    result = provider(report, "gemini")
    s = samples(result)["competitive-landscape-cloud"]
    assert code == 0 and s["synthesis"]["status"] == "error" and s["synthesis"]["score"] is None
    assert s["fact_check"]["pass_rate"] is not None and s["citations"]["accuracy"] is not None
    assert result["totals"]["synthesis"] == {"judged": 0, "errors": 1, "not_judged": 0,
                                             "mean_score": None}
    assert "invalid judge reply" in (run_dir / "eval-report.md").read_text(encoding="utf-8")


def test_without_a_judge_synthesis_is_not_judged_and_no_judge_is_built(tmp_path):
    judges = Judges()
    code, report, _ = evaluate(tmp_path, "--provider", "gemini", "--judge-provider", "none",
                               "--brief", "competitive-landscape-cloud", judges=judges)
    s = samples(provider(report, "gemini"))["competitive-landscape-cloud"]
    assert code == 0 and judges.built == []
    assert s["synthesis"]["status"] == "not_judged" and report["judge"]["model"] is None


# --- usage errors: nothing runs, nothing is written ------------------------------------------


def test_a_provider_without_its_key_is_refused_before_any_run(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    world = World()
    code, _, out = evaluate(tmp_path, "--provider", "gemini", "--judge-provider", "none",
                            world=world)
    assert code == 2 and not out.exists() and world.built == {}


def test_a_judge_without_its_key_is_refused_before_any_run(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    world = World()
    code, _, out = evaluate(tmp_path, "--provider", "gemini", "--judge-provider", "groq",
                            world=world)
    assert code == 2 and not out.exists() and world.built == {}


@pytest.mark.parametrize("args", [
    ("--provider", "gemini", "--judge-provider", "none", "--brief", "no-such-brief"),
    ("--provider", "gemini", "--provider", "gemini", "--judge-provider", "none"),
])
def test_bad_arguments_are_refused_before_any_run(tmp_path, args):
    world = World()
    code, _, out = evaluate(tmp_path, *args, world=world)
    assert code == 2 and not out.exists() and world.built == {}


class Unbuildable(World):
    """A world whose Deps cannot be built for one provider (a model or service that fails to
    construct after its Settings validated)."""

    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    def make_deps(self, settings, repos, checkpointer, qdrant):
        if settings.llm_provider == self.broken:
            raise RuntimeError("cannot construct the search client")
        return super().make_deps(settings, repos, checkpointer, qdrant)


def test_a_provider_whose_deps_cannot_be_built_stops_before_any_run(tmp_path, capsys):
    world = Unbuildable("groq")
    code, _, out = evaluate(tmp_path, "--provider", "gemini", "--provider", "groq",
                            "--judge-provider", "none", world=world)
    assert code == 1 and "groq" in capsys.readouterr().err
    # No partial evaluation is left behind; `--out` itself may hold earlier evaluations.
    assert list(out.iterdir()) == []
    # gemini's Deps were built, but nothing ran on them.
    gemini = world.built["gemini"]
    assert gemini.planner_model.seen == [] and gemini.writer_model.seen == []


def test_a_judge_that_cannot_be_built_stops_before_any_run(tmp_path, capsys):
    def broken(settings):
        raise RuntimeError("cannot construct the judge")
    world = World()
    out = tmp_path / "out"
    code = main(["--library", str(LIBRARY), "--out", str(out), "--provider", "gemini",
                 "--judge-provider", "groq"], make_deps=world.make_deps, make_judge=broken,
                root=ROOT)
    assert code == 1 and "cannot construct the judge" in capsys.readouterr().err
    assert not out.exists() and world.built == {}


def test_the_judge_provider_must_be_chosen_explicitly(tmp_path):
    with pytest.raises(SystemExit) as exited:
        evaluate(tmp_path, "--provider", "gemini")
    assert exited.value.code == 2
