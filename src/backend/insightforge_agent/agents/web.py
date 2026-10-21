"""Web search and page fetching behind small interfaces (design.md section 5, Web Search)."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlparse

import httpx

from insightforge_agent.config import Settings

logger = logging.getLogger(__name__)

MIN_BODY_CHARS = 200  # shorter than this is a paywall stub or an empty shell, not an article


class FetchError(Exception):
    """A page could not be fetched."""


class PaywalledError(FetchError):
    """A page is behind a paywall or login, or has no readable body."""


class SearchError(Exception):
    """Every search provider failed."""


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str = ""


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    body: str
    published_at: datetime | None = None  # from page metadata only; None when absent or invalid
    byline: str | None = None
    # The page's own stated modification date, only when it also states a publication date and
    # this is later: a page updated since (a statistics page) is then never shown by its
    # publication date alone. Never used as the publication date.
    updated_at: datetime | None = None


class SearchProvider(Protocol):
    def search(self, query: str, limit: int) -> list[SearchHit]: ...


class PageFetcher(Protocol):
    def fetch(self, url: str) -> Page:
        """Raises PaywalledError or FetchError."""
        ...


class TavilySearch:
    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        self._key = api_key
        self._timeout = timeout

    def search(self, query: str, limit: int) -> list[SearchHit]:
        r = httpx.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"query": query, "max_results": limit},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return [
            SearchHit(h["url"], h.get("title") or h["url"], h.get("content") or "")
            for h in r.json().get("results", [])
        ]


class FallbackSearch:
    """Tries each provider in order; a Run fails on search only when every one fails."""

    def __init__(self, providers: list[SearchProvider]) -> None:
        self._providers = providers

    def search(self, query: str, limit: int) -> list[SearchHit]:
        errors = []
        for p in self._providers:
            try:
                return p.search(query, limit)
            except Exception as e:  # noqa: BLE001 - any provider failure falls through
                logger.warning("search provider %s failed: %s", type(p).__name__, e)
                errors.append(f"{type(p).__name__}: {e}")
        raise SearchError("; ".join(errors) or "no search provider configured")


def build_search(settings: Settings) -> SearchProvider:
    """Providers in the configured order, skipping any this build cannot talk to yet."""
    built: list[SearchProvider] = []
    for name in settings.usable_search_providers:
        if name == "tavily":
            built.append(TavilySearch(settings.tavily_api_key))  # type: ignore[arg-type]
        else:
            logger.warning("Search provider %r is not implemented yet; skipped", name)
    if not built:
        raise SearchError("no implemented search provider is usable")
    return FallbackSearch(built)


class _TextExtractor(HTMLParser):
    _BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th"}
    _SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript", "form"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.meta: dict[str, str] = {}  # first non-empty value per lower-cased property / name / itemprop
        self.json_ld: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "title":
            self._in_title = True
        elif tag in self._SKIP:
            self._skip_depth += 1
            kind = next((v or "" for k, v in attrs if k.lower() == "type"), "")
            if tag == "script" and kind.strip().lower() == "application/ld+json":
                self._in_json_ld = True
                self.json_ld.append("")
        elif tag in self._BLOCKS or tag == "br":
            self._parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            if tag == "script":
                self._in_json_ld = False
        elif tag in self._BLOCKS:
            self._parts.append("\n\n")

    def handle_data(self, data):
        if self._in_json_ld:
            self.json_ld[-1] += data
        elif self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        paragraphs = (re.sub(r"\s+", " ", p).strip() for p in raw.split("\n\n"))
        return "\n\n".join(p for p in paragraphs if p)


# Only metadata that names the *publication* date. A bare "date" meta tag, dateModified,
# <time> elements, body text and URL paths are ambiguous and are never used for it.
# dateModified is recorded only as the separate `Page.updated_at`.
_DATE_META = ("article:published_time", "datepublished")
_MODIFIED_META = ("article:modified_time", "datemodified")
_AUTHOR_META = ("author", "article:author")
_EARLIEST_PLAUSIBLE = datetime(1990, 1, 1, tzinfo=UTC)


def _json_ld_objects(blocks: list[str]) -> list[dict]:
    """Every JSON-LD object on the page, nested ones included (lists, @graph, mainEntity, ...);
    unreadable blocks are skipped."""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            found.append(node)
            for value in node.values():
                walk(value)

    for block in blocks:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return found


def _types(obj: dict) -> list[str]:
    """Lower-cased schema.org type names, URL forms ("https://schema.org/NewsArticle") reduced."""
    raw = obj.get("@type")
    return [re.split(r"[/#:]", t)[-1].lower() for t in (raw if isinstance(raw, list) else [raw])
            if isinstance(t, str)]


def _is_article(obj: dict) -> bool:
    """Article and its subtypes (NewsArticle, TechArticle, BlogPosting, LiveBlogPosting, ...)."""
    return any(t.endswith(("article", "blogposting")) or t in ("report", "pressrelease")
               for t in _types(obj))


def _is_page(obj: dict) -> bool:
    return any(t.endswith("page") for t in _types(obj))


def _publication_objects(objects: list[dict]) -> list[dict]:
    """The objects whose datePublished and author describe the page: articles, then web pages.
    Anything else (ImageObject, VideoObject, Organization, WebSite, ...) is ignored."""
    return [o for o in objects if _is_article(o)] + [o for o in objects if _is_page(o) and not _is_article(o)]


def _parse_date(value: object, now: datetime) -> datetime | None:
    """An ISO 8601 date or date-time as an aware UTC datetime, or None when it is not one, lies
    before 1990 or after `now`. A date or naive time is read as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed if _EARLIEST_PLAUSIBLE <= parsed <= now else None


def _published_at(parser: _TextExtractor, objects: list[dict], now: datetime) -> datetime | None:
    """JSON-LD datePublished (articles, then pages), then article meta tags; first valid wins."""
    candidates = [o.get("datePublished") for o in objects] + [parser.meta.get(k) for k in _DATE_META]
    return next((d for d in (_parse_date(c, now) for c in candidates) if d), None)


def _updated_at(parser: _TextExtractor, objects: list[dict], now: datetime,
                published: datetime | None) -> datetime | None:
    """JSON-LD dateModified (the same objects), then modified-time meta tags; first valid wins,
    kept only when the page states a publication date and this is after it."""
    candidates = [o.get("dateModified") for o in objects] + [parser.meta.get(k)
                                                             for k in _MODIFIED_META]
    modified = next((d for d in (_parse_date(c, now) for c in candidates) if d), None)
    return modified if published is not None and modified is not None and modified > published \
        else None


def _author_name(author: object) -> str | None:
    """A named author. An Organization is a publisher, not a byline."""
    if isinstance(author, list):
        return next((n for n in map(_author_name, author) if n), None)
    if isinstance(author, dict):
        if "organization" in _types(author):
            return None
        author = author.get("name")
    return author.strip() if isinstance(author, str) and author.strip() else None


def _byline(parser: _TextExtractor, objects: list[dict]) -> str | None:
    candidates = [o.get("author") for o in objects] + [parser.meta.get(k) for k in _AUTHOR_META]
    return next((n for n in map(_author_name, candidates) if n), None)


def html_to_page(url: str, html: str, now: datetime | None = None) -> Page:
    """Title, readable body, and the publication date and byline from the page's metadata.
    A date after `now` (default: the current time) is treated as missing."""
    parser = _TextExtractor()
    parser.feed(html)
    objects = _publication_objects(_json_ld_objects(parser.json_ld))
    now = now or datetime.now(UTC)
    published = _published_at(parser, objects, now)
    return Page(url=url, title=re.sub(r"\s+", " ", parser.title).strip() or url, body=parser.text(),
                published_at=published, byline=_byline(parser, objects),
                updated_at=_updated_at(parser, objects, now, published))


class HttpFetcher:
    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    def fetch(self, url: str) -> Page:
        try:
            r = httpx.get(
                url, timeout=self._timeout, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; InsightForge/0.1)"},
            )
        except httpx.HTTPError as e:
            raise FetchError(f"{url}: {e}") from e
        if r.status_code in (401, 402, 403):
            raise PaywalledError(f"{url}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise FetchError(f"{url}: HTTP {r.status_code}")
        page = html_to_page(url, r.text)
        if len(page.body) < MIN_BODY_CHARS:
            raise PaywalledError(f"{url}: no readable body")
        return page


_TRUSTED_SUFFIXES = (".gov", ".edu", ".int")
# Wire services and national or international newspapers and broadcasters with editorial
# standards. Deliberately short; a domain matches itself and its subdomains only.
MAJOR_PRESS = frozenset({
    "reuters.com", "apnews.com", "afp.com", "bloomberg.com", "ft.com", "wsj.com",
    "nytimes.com", "washingtonpost.com", "economist.com", "theguardian.com", "bbc.com",
    "bbc.co.uk", "cnbc.com", "nikkei.com", "scmp.com", "lemonde.fr", "handelsblatt.com",
})


def _is_major_press(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in MAJOR_PRESS)


def credibility_score(
    url: str, published_at: datetime | None, now: datetime, *, has_byline: bool = False,
) -> float:
    """Assignment rubric, additive and explainable. Always within 0 to 1.

    - Domain type: .gov / .edu / .int 0.8; major press (`MAJOR_PRESS`) 0.7; any other 0.55.
    - Recency: +0.1 up to a year old, -0.1 over three years, 0 in between. An unknown date (or
      one after `now`) earns nothing either way.
    - Byline: +0.05 when the page names an author. Too small to lift an unknown domain to the
      MEDIUM threshold (0.65) on its own; it needs a recent date as well."""
    host = (urlparse(url).hostname or "").lower()
    score = 0.8 if host.endswith(_TRUSTED_SUFFIXES) else 0.7 if _is_major_press(host) else 0.55
    if published_at is not None and published_at <= now:
        age_days = (now - published_at).days
        score += 0.1 if age_days <= 365 else -0.1 if age_days > 3 * 365 else 0.0
    if has_byline:
        score += 0.05
    return round(max(0.0, min(1.0, score)), 2)
