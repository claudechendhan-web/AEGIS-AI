"""Search providers for the web tools.

The important design decision here is which providers are *legitimate*.

General web search has no free, keyless, official API. The providers that
advertise one without a key (scraping DuckDuckGo's or Bing's HTML endpoints)
breach their terms of service, and building on that is how an agent ends up
blocked and how a project ends up unusable by anyone else. So:

* `WikipediaSearch` uses the public MediaWiki API. It is a documented, free,
  keyless API that explicitly welcomes automated use, and it is a genuinely
  useful encyclopaedic corpus.
* `KeyedSearch` covers the real general-web providers (Brave, Tavily, Serper,
  Bing). These need an account and an API key, which is the correct state of
  affairs rather than a limitation to route around.
* `NullSearch` fails with a clear message, so a missing key is obvious instead
  of silently returning nothing and looking like "no results exist".

To enable general web search, set one of:

    BRAVE_SEARCH_API_KEY
    TAVILY_API_KEY
    SERPER_API_KEY
    BING_SEARCH_API_KEY
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_SEARCH = "https://en.wikipedia.org/w/restrict/v1/search/page"
DEFAULT_USER_AGENT = "AEGISAI/0.2 (local agent; respects robots.txt)"


class SearchError(Exception):
    """Raised when a search cannot be performed."""


class SearchCredentialsMissingError(SearchError):
    """Raised when a keyed provider is selected but no key is configured."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result."""

    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class SearchProvider(Protocol):
    """What every provider must offer."""

    name: str

    def search(self, query: str, *, limit: int = 5) -> Sequence[SearchHit]: ...


def _strip_html(text: str) -> str:
    """Wikipedia snippets arrive with <span class="searchmatch"> markers."""
    import re

    return re.sub(r"<[^>]+>", "", text).strip()


def _get_json(url: str, headers: Mapping[str, str], timeout: float) -> Any:
    request = Request(url, headers=dict(headers))
    response = None
    try:
        response = urlopen(request, timeout=timeout)
        raw = response.read()
    except HTTPError as exc:
        raise SearchError(f"HTTP {exc.code} from search provider") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SearchError(f"unable to reach search provider: {exc}") from exc
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SearchError("search provider returned invalid JSON") from exc


class WikipediaSearch:
    """Keyless search over Wikipedia via the public MediaWiki API."""

    name = "wikipedia"

    def __init__(
        self,
        *,
        limit: int = 5,
        timeout: float = 15.0,
        opener: Any = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.limit = max(1, limit)
        self.timeout = timeout
        self.user_agent = user_agent
        self._opener = opener or urlopen

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        count = max(1, min(limit or self.limit, 20))
        params = urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": count,
                "format": "json",
                "utf8": 1,
            }
        )
        url = f"{WIKIPEDIA_API}?{params}"
        if self._opener is not urlopen:
            payload = _get_json_via(self._opener, url, self.timeout, self.user_agent)
        else:
            payload = _get_json(url, {"User-Agent": self.user_agent}, self.timeout)
        pages = ((payload or {}).get("query") or {}).get("search") or []
        hits: list[SearchHit] = []
        for page in pages:
            if not isinstance(page, Mapping):
                continue
            title = str(page.get("title") or "")
            if not title:
                continue
            slug = title.replace(" ", "_")
            hits.append(
                SearchHit(
                    title=title,
                    url=f"https://en.wikipedia.org/wiki/{slug}",
                    snippet=_strip_html(str(page.get("snippet") or "")),
                )
            )
        return hits


def _get_json_via(opener: Any, url: str, timeout: float, user_agent: str) -> Any:
    """JSON GET through an injected opener, for tests."""
    request = Request(url, headers={"User-Agent": user_agent})
    response = None
    try:
        response = opener(request, timeout=timeout)
        raw = response.read()
    except HTTPError as exc:
        raise SearchError(f"HTTP {exc.code} from search provider") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SearchError(f"unable to reach search provider: {exc}") from exc
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SearchError("search provider returned invalid JSON") from exc


class KeyedSearch:
    """General web search via a keyed provider.

    Brave is the default because it has the most generous free tier, but
    Tavily is a better fit for agent use because it returns cleaned excerpts
    rather than raw snippets.
    """

    PROVIDERS: ClassVar[dict[str, tuple[str, str, str]]] = {
        "brave": (
            "BRAVE_SEARCH_API_KEY",
            "https://api.search.brave.com/res/v1/web/search",
            "web",
        ),
        "tavily": (
            "TAVILY_API_KEY",
            "https://api.tavily.com/search",
            "results",
        ),
        "serper": (
            "SERPER_API_KEY",
            "https://google.serper.dev/search",
            "organic",
        ),
        "bing": (
            "BING_SEARCH_API_KEY",
            "https://api.bing.microsoft.com/v7.0/search",
            "webPages",
        ),
    }

    def __init__(
        self,
        provider: str = "brave",
        *,
        api_key: str | None = None,
        limit: int = 5,
        timeout: float = 20.0,
        opener: Any = None,
    ) -> None:
        key = provider.strip().lower()
        if key not in self.PROVIDERS:
            options = ", ".join(sorted(self.PROVIDERS))
            raise SearchError(f"unknown search provider {provider!r}; try: {options}")
        self.name = key
        self._env_var, self._endpoint, self._shape = self.PROVIDERS[key]
        self.api_key = api_key or os.environ.get(self._env_var, "")
        if not self.api_key:
            raise SearchCredentialsMissingError(
                f"{key} search needs an API key. Set {self._env_var} in the "
                "environment, or use the keyless wikipedia provider."
            )
        self.limit = max(1, limit)
        self.timeout = timeout
        self._opener = opener or urlopen

    def search(self, query: str, *, limit: int | None = None) -> list[SearchHit]:
        count = max(1, min(limit or self.limit, 20))
        headers = {"Accept": "application/json"}
        body: dict[str, Any] = {"q": query, "count": count}
        if self.name == "brave":
            headers["X-Subscription-Token"] = self.api_key
            url = f"{self._endpoint}?{urlencode({'q': query, 'count': count})}"
            method = "GET"
        elif self.name == "tavily":
            headers["Content-Type"] = "application/json"
            headers["Authorization"] = f"Bearer {self.api_key}"
            url = self._endpoint
            method = "POST"
            body = {
                "api_key": self.api_key,
                "query": query,
                "max_results": count,
                "search_depth": "basic",
            }
        elif self.name == "serper":
            headers["X-API-KEY"] = self.api_key
            headers["Content-Type"] = "application/json"
            url = self._endpoint
            method = "POST"
        else:
            headers["Ocp-Apim-Subscription-Key"] = self.api_key
            url = f"{self._endpoint}?{urlencode({'q': query, 'count': count})}"
            method = "GET"

        payload = self._request(url, headers, body if method == "POST" else None)
        return self._parse(payload, count)

    def _request(
        self, url: str, headers: Mapping[str, str], body: Mapping[str, Any] | None
    ) -> Any:
        data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
        request = Request(
            url, data=data, headers=dict(headers), method="POST" if data else "GET"
        )
        response = None
        try:
            response = self._opener(request, timeout=self.timeout)
            raw = response.read()
        except HTTPError as exc:
            raise SearchError(f"HTTP {exc.code} from {self.name} search") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SearchError(f"unable to reach {self.name} search: {exc}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SearchError(f"{self.name} returned invalid JSON") from exc

    def _parse(self, payload: Any, limit: int) -> list[SearchHit]:
        container: Any = payload
        for key in self._shape.split("."):
            container = (
                (container or {}).get(key) if isinstance(container, Mapping) else None
            )
        if isinstance(container, Mapping):
            container = container.get("value") or []
        if not isinstance(container, list):
            return []
        hits: list[SearchHit] = []
        for item in container[:limit]:
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or item.get("link") or "")
            if not url:
                continue
            hits.append(
                SearchHit(
                    title=str(item.get("title") or url),
                    url=url,
                    snippet=_strip_html(
                        str(
                            item.get("snippet")
                            or item.get("description")
                            or item.get("content")
                            or ""
                        )
                    ),
                )
            )
        return hits


class NullSearch:
    """A provider that refuses, so a missing key is loud rather than silent."""

    name = "none"

    def __init__(self, reason: str = "no search provider is configured") -> None:
        self.reason = reason

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        raise SearchCredentialsMissingError(self.reason)


def build_search_provider(
    preferred: str | None = None, *, timeout: float = 15.0
) -> SearchProvider:
    """Choose a provider: an explicit preference, then any configured key, then Wikipedia."""
    if preferred:
        key = preferred.strip().lower()
        if key == "wikipedia":
            return WikipediaSearch(timeout=timeout)
        if key in KeyedSearch.PROVIDERS:
            return KeyedSearch(key, timeout=timeout)
        if key in ("none", "null"):
            return NullSearch()
        options = "wikipedia, " + ", ".join(sorted(KeyedSearch.PROVIDERS)) + ", none"
        raise SearchError(f"unknown search provider {preferred!r}; try: {options}")
    for candidate in ("brave", "tavily", "serper", "bing"):
        if os.environ.get(KeyedSearch.PROVIDERS[candidate][0]):
            return KeyedSearch(candidate, timeout=timeout)
    return WikipediaSearch(timeout=timeout)


def describe_providers() -> list[dict[str, Any]]:
    """What is configured right now. Useful for a health check."""
    rows = [
        {
            "provider": "wikipedia",
            "requires_key": False,
            "configured": True,
            "env_var": None,
        }
    ]
    for name, (env_var, _endpoint, _shape) in sorted(KeyedSearch.PROVIDERS.items()):
        rows.append(
            {
                "provider": name,
                "requires_key": True,
                "configured": bool(os.environ.get(env_var)),
                "env_var": env_var,
            }
        )
    return rows
