"""T18: the six remediation changes together, through the production `eval` path, on the whole
five-brief sample library (web, uploaded document and memory briefs), with scripted models.

The same evaluation is run with every T18 behaviour on (native structured drafting, a drafting
Passage cap, the counterpart fallback; provenance, document recovery and the Writer changes have
no switch) and with the switchable ones off, and checks the invariants that must hold across
them: memory and document isolation, historical Claims kept apart from current Evidence, no
Claim verified without an entailment SUPPORTED, document Claims never re-fetched, gap lines that
never call retrieval verified, and every behaviour recorded in the report's conditions."""

import json
import re
from pathlib import Path

import pytest

from insightforge_agent.evaluation.cli import main
from insightforge_agent.stores.sqlite import SqliteRepos
from tests.evaluation.world import World

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "samples" / "briefs"


class Native:
    """Stands in for llm.native_json: records the schemas, answers through the model."""

    def __init__(self):
        self.schemas = []

    def __call__(self, model, schema):
        self.schemas.append(schema.__name__)

        class Bound:
            def invoke(self, messages):
                return {"raw": model.invoke(messages), "parsed": None, "parsing_error": None}
        return Bound()


def evaluate(tmp, overrides):
    mp = pytest.MonkeyPatch()
    for key in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY"):
        mp.setenv(key, "test-key-not-used")
    mp.setenv("SEARCH_PROVIDERS", "tavily")
    mp.chdir(tmp)
    world = World(overrides={"anthropic": overrides})
    try:
        code = main(["--library", str(LIBRARY), "--out", str(tmp / "out"), "--provider",
                     "anthropic", "--judge-provider", "none"], make_deps=world.make_deps,
                    root=ROOT)
    finally:
        mp.undo()
    assert code == 0
    [run_dir] = list((tmp / "out").iterdir())
    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    return report, world, SqliteRepos(str(run_dir / "anthropic" / "eval.db"))


@pytest.fixture(scope="module")
def on(tmp_path_factory):
    native = Native()
    report, world, repos = evaluate(tmp_path_factory.mktemp("on"), {
        "native_json": native, "draft_passage_cap": 2, "fact_check_counterpart": True})
    return report, world, repos, native


@pytest.fixture(scope="module")
def off(tmp_path_factory):
    return evaluate(tmp_path_factory.mktemp("off"), {
        "native_json": None, "draft_passage_cap": None, "fact_check_counterpart": False})


def samples(report):
    return {s["sample_id"]: s for s in report["providers"][0]["samples"]}


def events(repos, owner, run_id, type_=None):
    out, cursor = [], 0
    while batch := repos.events.read_after(owner, run_id, cursor):
        out += batch
        cursor = batch[-1].seq
    return [e for e in out if type_ is None or e.type == type_]


def test_every_brief_completes_and_the_report_records_each_t18_behaviour(on, off):
    for report, expected in ((on[0], ("on", 2, "support-2+cited")),
                             (off[0], ("off", None, "support-2"))):
        assert {s["status"] for s in samples(report).values()} == {"complete"}
        c = report["providers"][0]["conditions"]
        assert (c["draft_structured_output"], c["draft_passage_cap"], c["fact_check_method"],
                c["writer_prompt_version"]) == (*expected, "writer-3")


def test_only_extraction_used_the_native_path_and_each_call_held_at_most_the_cap(on):
    _, world, _, native = on
    assert native.schemas and set(native.schemas) == {"_Facts"}
    light = world.built["anthropic"].light_model
    extractions = [str(c[-1].content) for c in light.seen
                   if str(c[0].content).startswith("You extract facts")]
    assert extractions and all(len(re.findall(r"^\[\d+\] ", d, re.MULTILINE)) <= 2
                               for d in extractions)


def test_switched_off_the_fallback_never_runs(off):
    report, _, repos = off
    for s in samples(report).values():
        assert events(repos, s["owner_id"], s["run_id"], "fact_check_counterpart") == []


@pytest.mark.parametrize("which", ["on", "off"])
def test_no_claim_is_verified_without_a_supported_judgment(which, on, off):
    report, _, repos = (on[0], on[1], on[2]) if which == "on" else off
    for s in samples(report).values():
        body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
        supported = {(e.payload["section"], e.payload["claim"])
                     for e in events(repos, s["owner_id"], s["run_id"], "fact_check_support")
                     if "SUPPORTED" in e.payload["verdicts"]}
        for v in body["verified"]["verdicts"]:
            if v["verdict"] == "verified":
                assert (v["section"], v["claim"]) in supported, (s["sample_id"], v)


def test_document_claims_are_checked_against_stored_chunks_never_by_counterpart(on):
    report, _, repos, _ = on
    s = samples(report)["document-annual-report"]
    body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
    pdf = {(v["section"], v["claim"]) for v in body["verified"]["verdicts"]
           if v["check"].endswith("document_passages")}
    attempts = {(e.payload["section"], e.payload["claim"])
                for e in events(repos, s["owner_id"], s["run_id"], "fact_check_counterpart")}
    assert pdf.isdisjoint(attempts)


def test_each_reports_evidence_comes_only_from_its_own_users_runs(on):
    """Document and memory isolation: every cited Passage belongs to this sample's User."""
    report, _, repos, _ = on
    for s in samples(report).values():
        body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
        refs = [r for sec in body["evidence"]["sections"] for i in sec["items"]
                for r in i["supporting"]]
        refs += [i["primary"] for i in (body.get("historical") or {}).get("items", [])]
        for r in refs:
            obs = repos.observations.get(s["owner_id"], r["observation_id"])  # raises if foreign
            assert obs.owner_id == s["owner_id"]


def test_historical_claims_stay_apart_from_current_evidence(on):
    report, _, repos, _ = on
    s = samples(report)["memory-prior-report"]
    body = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body
    current = {i["id"] for sec in body["evidence"]["sections"] for i in sec["items"]}
    background = {i["id"] for i in (body.get("historical") or {}).get("items", [])}
    claims = [c for sec in body["draft"]["sections"] for c in sec["claims"]]
    assert any(c["historical"] for c in claims)
    for c in claims:
        assert (c["evidence_id"] in background) is c["historical"]
        assert (c["evidence_id"] in current) is (not c["historical"])
        if c["historical"]:
            assert re.search(r"\(reported \d{4}-\d{2}-\d{2}\)", c["text"])
    # Memory is this User's own earlier Reports (the sample's setup Runs), never another's.
    own = {r.id for r in repos.reports.list_for_owner(s["owner_id"])}
    assert {i["report_id"] for i in body["historical"]["items"]} <= own - {s["report_id"]}


def test_gap_lines_never_call_retrieval_verified_or_a_source_lacking(on):
    report, _, repos, _ = on
    for s in samples(report).values():
        gaps = repos.reports.get_for_run(s["owner_id"], s["run_id"]).body["draft"]["gaps"]
        text = " ".join(gaps).lower()
        assert "verified" not in text and "confirmed" not in text
        assert "does not contain" not in text and "does not disclose" not in text


def test_the_writer_saw_period_or_publication_date_on_every_evidence_line(on):
    _, world, _, _ = on
    writer = world.built["anthropic"].writer_model
    payloads = [str(c[-1].content) for c in writer.seen if "Evidence bundle:" in str(c[-1].content)]
    lines = [ln for p in payloads for ln in p.splitlines() if ln.startswith("- [ev_")]
    assert lines and all(re.match(r"- \[ev_\w+\] \((HIGH|MEDIUM|LOW); (as of |period not stated)",
                                   ln) for ln in lines)



def test_the_report_states_the_fact_check_paths_beside_the_pass_rate(on, off):
    for report in (on[0], off[0]):
        for s in samples(report).values():
            paths = s["fact_check_paths"]
            assert paths["verified_by_counterpart"] <= paths["counterpart_judged"] <= paths[
                "counterpart_looked_up"]
    assert all(s["fact_check_paths"]["counterpart_looked_up"] == 0
               for s in samples(off[0]).values())
