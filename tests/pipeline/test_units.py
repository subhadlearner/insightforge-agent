import pytest

from insightforge_agent.agents.web import (
    FallbackSearch,
    SearchError,
    SearchHit,
    credibility_score,
    html_to_page,
)
from insightforge_agent.domain.contracts import Brief
from insightforge_agent.domain.models import Passage, RunState
from insightforge_agent.domain.passages import batch_passages, numbers_in
from insightforge_agent.domain.tokens import estimate_tokens
from insightforge_agent.pipeline.graph import run_brief
from tests.pipeline import fakes
from tests.pipeline.test_happy_path import BRIEF
from datetime import UTC, datetime


def passages(*texts):
    return [Passage(observation_id="o", index=i, text=t) for i, t in enumerate(texts)]


def test_batches_split_at_the_cap_and_keep_every_passage_whole():
    ps = passages("a" * 400, "b" * 400, "c" * 400)  # about 100 tokens each
    batches = batch_passages(ps, 250)
    assert [[w.passage.index for w in b] for b in batches] == [[0, 1], [2]]
    assert [w.passage for b in batches for w in b] == ps
    assert all(w.parts == 1 and w.text == w.passage.text for b in batches for w in b)


def test_an_oversized_passage_is_read_in_windows_that_fit_the_cap_and_lose_nothing():
    words = [f"w{i}" for i in range(400)]
    big = passages(" ".join(words))
    batches = batch_passages(big, 100)
    windows = [w for b in batches for w in b]
    assert len(windows) > 1 and len(batches) > 1
    assert all(estimate_tokens(w.text) <= 100 for w in windows)
    assert all(estimate_tokens("".join(w.text for w in b)) <= 100 for b in batches)
    assert " ".join(w.text for w in windows).split() == words  # nothing truncated
    assert {w.passage for w in windows} == set(big)  # identity untouched
    assert [(w.part, w.parts) for w in windows] == [(n, len(windows)) for n in range(1, len(windows) + 1)]


def test_an_unbreakable_oversized_passage_is_still_windowed():
    windows = [w for b in batch_passages(passages("x" * 4000), 100) for w in b]
    assert all(estimate_tokens(w.text) <= 100 for w in windows)
    assert "".join(w.text for w in windows) == "x" * 4000


def test_extraction_payloads_respect_the_per_call_cap(make_deps):
    light = fakes.light()
    deps = make_deps(light_model=light, passage_token_cap=30)
    assert run_brief(deps, "alice", Brief(text=BRIEF)).state == RunState.COMPLETE
    extraction_calls = [c for c in light.seen if str(c[0].content).startswith("You extract")]
    assert len(extraction_calls) > 3  # the small cap forced splitting
    for conv in extraction_calls:
        passage_text = str(conv[-1].content).split("\n\n", 1)[1]
        # every fixture paragraph is under the cap on its own, so a batch must fit it
        assert estimate_tokens(passage_text) <= 30


def test_stored_evidence_bundle_fits_the_budget_and_each_cut_is_logged(make_deps):
    from insightforge_agent.domain.contracts import EvidenceBundle
    from insightforge_agent.pipeline.synthesize import bundle_tokens

    first = make_deps()
    full_run = run_brief(first, "alice", Brief(text=BRIEF))
    full = EvidenceBundle.model_validate(
        first.repos.reports.get_for_run("alice", full_run.id).body["evidence"])
    budget = bundle_tokens(full) - 10
    deps = make_deps(evidence_budget_tokens=budget)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    assert run.state == RunState.COMPLETE
    stored = EvidenceBundle.model_validate(deps.repos.reports.get_for_run("alice", run.id).body["evidence"])
    assert bundle_tokens(stored) <= budget
    cuts = [e for e in deps.repos.events.read_after("alice", run.id) if e.type == "evidence_cut"]
    assert cuts and all({"reason", "confidence", "evidence_id"} <= set(e.payload) for e in cuts)


def test_a_statement_with_a_number_not_in_its_passage_is_dropped(make_deps):
    light = fakes.light()
    plain = light.reply

    def lying(messages):
        msg = plain(messages)
        return msg.model_copy(update={"content": msg.content.replace("4.27", "9.99")})

    light.reply = lying
    deps = make_deps(light_model=light)
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    events = deps.repos.events.read_after("alice", run.id)
    assert any(e.type == "evidence_dropped" and "9.99" in str(e.payload["rejected"])
               for e in events)
    report = deps.repos.reports.get_for_run("alice", run.id)
    assert "9.99" not in str(report.body)


def test_two_domains_give_high_confidence_one_gives_medium_or_low(make_deps):
    deps = make_deps()
    run = run_brief(deps, "alice", Brief(text=BRIEF))
    report = deps.repos.reports.get_for_run("alice", run.id)
    items = [i for s in report.body["evidence"]["sections"] for i in s["items"]]
    assert {i["confidence"] for i in items} <= {"HIGH", "MEDIUM", "LOW"}
    for i in items:
        assert i["primary"] in i["supporting"]
        assert i["as_of_period"] == "UNKNOWN"


def test_numbers_are_compared_without_thousands_separators():
    assert numbers_in("sold 4,270,000 cars at 32,000 dollars, 4.27 million") >= {
        "4270000", "32000", "4.27"}


def test_html_is_reduced_to_paragraphs_without_chrome():
    page = html_to_page("https://x.test/a", """<html><head><title> Hello  World </title>
        <style>p{}</style></head><body><nav>menu</nav><h1>Head</h1><p>First   para.</p>
        <script>var x=1</script><p>Second para.</p><footer>foot</footer></body></html>""")
    assert page.title == "Hello World"
    assert page.body == "Head\n\nFirst para.\n\nSecond para."


def test_search_falls_through_to_the_next_provider():
    class Down:
        def search(self, q, n):
            raise RuntimeError("quota")

    class Up:
        def search(self, q, n):
            return [SearchHit("https://x.test", "X")]

    assert FallbackSearch([Down(), Up()]).search("q", 5)[0].url == "https://x.test"
    with pytest.raises(SearchError):
        FallbackSearch([Down(), Down()]).search("q", 5)


def test_credibility_is_within_zero_to_one():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    for url in ("https://a.gov/x", "https://a.com/x"):
        for pub in (None, datetime(2026, 1, 1, tzinfo=UTC), datetime(2015, 1, 1, tzinfo=UTC)):
            assert 0 <= credibility_score(url, pub, now) <= 1
    assert credibility_score("https://a.gov/x", None, now) > credibility_score("https://a.com/x", None, now)


def test_json_reply_ignores_thinking_blocks():
    from langchain_core.messages import AIMessage
    from pydantic import BaseModel

    from insightforge_agent.agents.llm_json import ask_json
    from tests.scripted import ScriptedChat

    class M(BaseModel):
        n: int

    reply = AIMessage(content=[{"type": "thinking", "thinking": "hm", "signature": "abc"},
                               {"type": "text", "text": '```json\n{"n": 3}\n```'}])
    assert ask_json(ScriptedChat(script=[reply]), "s", "u", M).n == 3
