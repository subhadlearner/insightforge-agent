"""F1: publication date and byline read from page metadata, and the credibility they feed.

A date is taken only from metadata that names the publication date (JSON-LD `datePublished`,
`article:published_time` and the equivalent `itemprop`). Missing, unreadable or implausible
metadata gives no date: nothing is guessed from body text, the URL or the fetch time."""

import json
from datetime import UTC, datetime

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from insightforge_agent.agents.researcher import make_web_search_tool
from insightforge_agent.agents.web import SearchHit, credibility_score, html_to_page
from insightforge_agent.domain.evidence import Thresholds, confidence_of

NOW = datetime(2026, 10, 9, tzinfo=UTC)
BODY = "<p>" + "BYD sold 4.27 million vehicles in 2025. " * 8 + "</p>"


def html(head: str = "", body: str = BODY) -> str:
    return f"<html><head><title>T</title>{head}</head><body>{body}</body></html>"


def ld(obj) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


# --- publication date -------------------------------------------------------------------------

@pytest.mark.parametrize("head,expected", [
    ('<meta property="article:published_time" content="2026-03-02T08:15:00Z">',
     datetime(2026, 3, 2, 8, 15, tzinfo=UTC)),
    ('<meta property="article:published_time" content="2026-03-02T10:15:00+02:00">',
     datetime(2026, 3, 2, 8, 15, tzinfo=UTC)),
    ('<meta itemprop="datePublished" content="2026-03-02">', datetime(2026, 3, 2, tzinfo=UTC)),
    (ld({"@type": "NewsArticle", "datePublished": "2026-03-02T08:15:00Z"}),
     datetime(2026, 3, 2, 8, 15, tzinfo=UTC)),
    (ld({"@graph": [{"@type": "WebSite"}, {"@type": "Article", "datePublished": "2026-03-02"}]}),
     datetime(2026, 3, 2, tzinfo=UTC)),
    (ld([{"@type": "BlogPosting", "datePublished": "2026-03-02"}]), datetime(2026, 3, 2, tzinfo=UTC)),
])
def test_publication_date_is_read_from_article_metadata(head, expected):
    page = html_to_page("https://x.test/a", html(head), now=NOW)
    assert page.published_at == expected
    assert page.published_at.tzinfo is not None


def test_structured_data_wins_over_meta_tags():
    head = ('<meta property="article:published_time" content="2025-01-01">'
            + ld({"@type": "NewsArticle", "datePublished": "2026-03-02"}))
    assert html_to_page("https://x.test/a", html(head), now=NOW).published_at == datetime(2026, 3, 2, tzinfo=UTC)


def test_an_article_date_is_preferred_to_a_page_date_in_structured_data():
    head = ld({"@graph": [{"@type": "WebPage", "datePublished": "2020-01-01"},
                          {"@type": "NewsArticle", "datePublished": "2026-03-02"}]})
    assert html_to_page("https://x.test/a", html(head), now=NOW).published_at == datetime(2026, 3, 2, tzinfo=UTC)


@pytest.mark.parametrize("head,body", [
    ("", BODY),  # no metadata at all
    ('<meta property="article:published_time" content="">', BODY),
    ('<meta property="article:published_time" content="last Tuesday">', BODY),
    ('<meta property="article:published_time" content="2026-02-30">', BODY),
    ('<meta property="article:published_time" content="1850-01-01">', BODY),  # implausible
    ('<meta property="article:published_time" content="2027-01-01">', BODY),  # after the fetch
    ('<script type="application/ld+json">{not json</script>', BODY),
    (ld({"@type": "NewsArticle", "datePublished": 20260302}), BODY),
    (ld({"@type": "NewsArticle", "dateModified": "2026-03-02"}), BODY),  # modified is not published
    ('<meta name="date" content="2026-03-02">', BODY),  # ambiguous: not a publication date
    ("", "<p>Published 2 March 2026.</p>" + BODY),  # dates in the body text are never used
    ("", '<time datetime="2026-03-02">2 March</time>' + BODY),
])
def test_no_date_is_invented_when_metadata_is_missing_or_invalid(head, body):
    page = html_to_page("https://x.test/2026/03/02/story", html(head, body), now=NOW)
    assert page.published_at is None


@pytest.mark.parametrize("head", [
    ld({"@type": "WebPage", "datePublished": "2015-01-01",
        "mainEntity": {"@type": "NewsArticle", "datePublished": "2026-03-02"}}),
    ld({"@type": "https://schema.org/TechArticle", "datePublished": "2026-03-02"}),
    ld({"@graph": [{"@type": "ImageObject", "datePublished": "2018-01-01"},
                   {"@type": "LiveBlogPosting", "datePublished": "2026-03-02"}]}),
    '<meta property="article:published_time" content=""><meta property="article:published_time" content="2026-03-02">',
])
def test_the_article_date_is_found_wherever_the_page_puts_it(head):
    assert html_to_page("https://x.test/a", html(head), now=NOW).published_at == datetime(2026, 3, 2, tzinfo=UTC)


def test_a_media_object_date_is_not_a_publication_date():
    head = ld({"@graph": [{"@type": "ImageObject", "datePublished": "2026-03-02"}]})
    assert html_to_page("https://x.test/a", html(head), now=NOW).published_at is None


def test_json_ld_is_not_part_of_the_body_text():
    page = html_to_page("https://x.test/a", html(ld({"@type": "Article", "datePublished": "2026-03-02"})),
                        now=NOW)
    assert "datePublished" not in page.body


# --- byline -----------------------------------------------------------------------------------

@pytest.mark.parametrize("head,expected", [
    ('<meta name="author" content=" Jane Doe ">', "Jane Doe"),
    ('<meta property="article:author" content="https://x.test/staff/jane">', "https://x.test/staff/jane"),
    (ld({"@type": "NewsArticle", "author": "Jane Doe"}), "Jane Doe"),
    (ld({"@type": "NewsArticle", "author": {"@type": "Person", "name": "Jane Doe"}}), "Jane Doe"),
    (ld({"@type": "NewsArticle", "author": [{"name": "Jane Doe"}, {"name": "Li Wei"}]}), "Jane Doe"),
    ('<meta name="author" content=""><meta name="author" content="Jane Doe">', "Jane Doe"),
])
def test_byline_is_read_from_metadata(head, expected):
    assert html_to_page("https://x.test/a", html(head), now=NOW).byline == expected


@pytest.mark.parametrize("head", [
    "", '<meta name="author" content="  ">', ld({"@type": "NewsArticle", "author": {"name": ""}}),
    ld({"@type": "NewsArticle", "author": {"@type": "Organization", "name": "Acme Blog"}}),
    ld({"@type": "ImageObject", "author": "Photo Desk"}),
])
def test_no_byline_without_author_metadata(head):
    assert html_to_page("https://x.test/a", html(head), now=NOW).byline is None


# --- credibility rubric -----------------------------------------------------------------------

FRESH, TWO_YEARS, OLD = (datetime(2026, 5, 1, tzinfo=UTC), datetime(2024, 10, 1, tzinfo=UTC),
                         datetime(2019, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize("url", ["https://www.reuters.com/a", "https://apnews.com/a",
                                 "https://www.bbc.co.uk/news/a", "https://markets.ft.com/a"])
def test_major_press_outranks_an_unknown_domain(url):
    assert credibility_score(url, None, NOW) > credibility_score("https://example.com/a", None, NOW)


def test_a_lookalike_domain_is_not_major_press():
    for url in ("https://notreuters.com/a", "https://reuters.com.example.net/a"):
        assert credibility_score(url, None, NOW) == credibility_score("https://example.com/a", None, NOW)


def test_byline_adds_a_little_and_never_crosses_medium_alone():
    high = Thresholds().credibility_high
    blog = "https://example.com/a"
    assert credibility_score(blog, None, NOW, has_byline=True) > credibility_score(blog, None, NOW)
    assert credibility_score(blog, None, NOW, has_byline=True) < high
    assert credibility_score(blog, TWO_YEARS, NOW, has_byline=True) < high


def test_recency_decays_and_unknown_age_earns_nothing():
    url = "https://example.com/a"
    scores = [credibility_score(url, d, NOW) for d in (FRESH, TWO_YEARS, OLD)]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]
    assert credibility_score(url, None, NOW) == credibility_score(url, TWO_YEARS, NOW)


def test_a_date_after_now_earns_no_recency_bonus():
    url = "https://example.com/a"
    assert credibility_score(url, datetime(2027, 1, 1, tzinfo=UTC), NOW) == credibility_score(url, None, NOW)


def test_scores_stay_within_zero_and_one():
    for url in ("https://a.gov/x", "https://www.reuters.com/x", "https://a.com/x"):
        for pub in (None, FRESH, TWO_YEARS, OLD):
            for byline in (False, True):
                assert 0 <= credibility_score(url, pub, NOW, has_byline=byline) <= 1


# --- downstream: what the Researcher stores and the Confidence it allows ------------------------

PAGES = {
    "https://www.reuters.com/byd": html(
        '<meta property="article:published_time" content="2026-09-20T06:00:00Z">'
        '<meta name="author" content="Jane Doe">'),
    "https://blog.example.com/byd": html(),  # no date, no byline, unknown domain
}


class _Web:
    def search(self, query, limit):
        return [SearchHit(u, "T") for u in PAGES][:limit]

    def fetch(self, url):
        return html_to_page(url, PAGES[url], now=NOW)


def test_the_researcher_stores_the_date_and_score_and_only_the_credible_recent_source_is_medium(make_deps):
    deps = make_deps()
    tool = make_web_search_tool(
        owner_id="alice", run_id="r", search=_Web(), fetcher=_Web(), store=deps.store,
        summarizer=FakeListChatModel(responses=["summary"] * 4), events=deps.repos.events,
        now=lambda: NOW, passage_token_cap=deps.passage_token_cap)
    refs = json.loads(tool.invoke({"query": "BYD sales"}))
    sources = {s.locator: s for s in (deps.repos.sources.get("alice", r["source_id"]) for r in refs)}
    press, blog = sources["https://www.reuters.com/byd"], sources["https://blog.example.com/byd"]

    assert press.published_at == datetime(2026, 9, 20, 6, tzinfo=UTC)
    assert blog.published_at is None
    assert press.credibility_score == credibility_score(press.locator, press.published_at, NOW, has_byline=True)

    high = Thresholds().credibility_high
    single = dict(has_independent_pair=False, conflicting=False, high=high)
    assert confidence_of(best_credibility=press.credibility_score, **single) == "MEDIUM"
    assert confidence_of(best_credibility=blog.credibility_score, **single) == "LOW"
    # the policy itself is unchanged: a conflict still forces LOW
    assert confidence_of(has_independent_pair=False, best_credibility=press.credibility_score,
                         conflicting=True, high=high) == "LOW"
