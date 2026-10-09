"""Web search and page fetching behind small interfaces (design.md section 5, Web Search)."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
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
    published_at: datetime | None = None


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
        self._in_title = False
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCKS or tag == "br":
            self._parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCKS:
            self._parts.append("\n\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        paragraphs = (re.sub(r"\s+", " ", p).strip() for p in raw.split("\n\n"))
        return "\n\n".join(p for p in paragraphs if p)


def html_to_page(url: str, html: str) -> Page:
    parser = _TextExtractor()
    parser.feed(html)
    return Page(url=url, title=re.sub(r"\s+", " ", parser.title).strip() or url, body=parser.text())


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


def credibility_score(url: str, published_at: datetime | None, now: datetime) -> float:
    """A first, simple score from domain type and recency (byline needs the page markup and
    comes with the full credibility work). Always within 0 to 1."""
    host = (urlparse(url).hostname or "").lower()
    score = 0.8 if host.endswith(_TRUSTED_SUFFIXES) else 0.6
    if published_at is not None:
        age_days = (now - published_at).days
        score += 0.1 if age_days <= 365 else -0.1 if age_days > 3 * 365 else 0.0
    return max(0.0, min(1.0, score))
